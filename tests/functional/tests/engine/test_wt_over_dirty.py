#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
A write-through write over dirty cache lines must not make them clean:
the core copy is not durable before the next core flush, and clean lines are
dropped by recovery after a dirty shutdown.

Handwritten crash timelines. Crash model: quiesce (settle), copy the cache
device, drop every core write not covered by a completed core flush, load the
cache and read back. Per block: OK = last flushed write or a newer write,
BUG = older content (or never written).
"""

import gc
import threading
import time
from ctypes import memmove

import pytest

from pyocf.types.cache import Cache, CacheMode, CacheLineSize, CleaningPolicy
from pyocf.types.core import Core
from pyocf.types.data import Data
from pyocf.types.io import IoDir, Sync
from pyocf.types.volume import RamVolume
from pyocf.types.volume_core import CoreVolume
from pyocf.utils import Size

from .wt_crash_devices import VolatileVolume, GuardedRam, pattern, BLK

CACHE_SIZE = Size.from_MiB(40)
CORE_BLOCKS = 1024
ZERO = bytes(BLK)


class Rig:
    n = 0
    cache_dev_type = GuardedRam  # test_cache_fua.py: VolatileCache
    fua_flag = 0  # I/O flag bit the volatile devices treat as FUA

    def __init__(self, ctx, mode=CacheMode.WB, ncores=1, cls1=None,
                 line_size=CacheLineSize.DEFAULT):
        Rig.n += 1
        self.ctx = ctx
        for t in (VolatileVolume, self.cache_dev_type):
            if t not in ctx.ocf_volume_type:
                ctx.register_volume_type(t)
        self.cache_ram = self.cache_dev_type(CACHE_SIZE)
        self.cache_ram.fua_flag = self.fua_flag
        self.uuids = ["wtod-%d-%d" % (Rig.n, i) for i in range(ncores)]
        self.drives = [VolatileVolume(CORE_BLOCKS * BLK, uuid=u) for u in self.uuids]
        for d in self.drives:
            d.fua_flag = self.fua_flag
        self.cache = Cache.start_on_device(self.cache_ram, cache_mode=mode,
                                           cache_line_size=line_size)
        self.cache.set_cleaning_policy(CleaningPolicy.NOP)
        self.cores, self.vols = [], []
        for i, d in enumerate(self.drives):
            c = Core.using_device(d, name="core%d" % i)
            self.cache.add_core(c)
            v = CoreVolume(c)
            v.open()
            self.cores.append(c)
            self.vols.append(v)
        if cls1 is not None:
            self.set_class(1, cls1)
        self.queue = self.cache.get_default_queue()
        self.seq = 0
        self.last_err = 0

    def set_class(self, cid, mode):
        self.cache.configure_partition(part_id=cid, name="class%d" % cid, max_size=100,
                                       priority=1, cache_mode=mode)

    def write(self, blk, nblk=1, core=0, io_class=0, flags=0):
        """Returns the content written: one block, or a list for nblk > 1"""
        self.seq += 1
        vals = [pattern(self.seq, b) for b in range(blk, blk + nblk)]
        data = Data.from_bytes(b"".join(vals))
        io = self.vols[core].new_io(self.queue, blk * BLK, nblk * BLK, IoDir.WRITE, io_class, flags)
        io.set_data(data)
        self.last_err = Sync(io).submit().results["err"]
        return vals[0] if nblk == 1 else vals

    def bflush(self, core=0):
        """Flush issued by the user of the block device"""
        io = self.vols[core].new_io(self.queue, 0, 0, IoDir.WRITE, 0, 0)
        err = Sync(io).submit_flush().results["err"]
        assert err == 0, err

    def read(self, blk, core=0):
        return self.vols[core].read_sync(self.queue, blk * BLK, BLK)

    def dirty_lines(self, core=0):
        return self.cores[core].get_stats()["usage"]["dirty"]["value"]

    def persist_metadata(self, back=CacheMode.WT):
        """Cache is in WT mode unless back says otherwise.

        Metadata changes made in RAM only (e.g. by error handling) reach the
        cache device with the next metadata flush of the same page: a WB write
        to a neighbouring line (block 16) forces one.
        """
        self.cache.change_cache_mode(CacheMode.WB)
        self.write(16)
        self.cache.change_cache_mode(back)

    def crash(self, clean_stop=False):
        """
        Quiesce, power cut, reload. With clean_stop the cache is stopped
        first, so the cache device holds the metadata saved by the stop.
        """
        self.cache.settle()
        time.sleep(0.2)
        self.cache.settle()
        for d in self.drives:
            d.release_all()
        for v in self.vols:
            v.close()
        if clean_stop:
            self.cache.stop()
        cache_bytes = self.cache_ram.snapshot()
        durables = [bytes(d.durable) for d in self.drives]
        if not clean_stop:
            self.cache.stop()
        d = v = None
        for u in self.uuids:
            VolatileVolume._uuid_.pop(u, None)
        self.cache = self.vols = self.cores = self.drives = self.cache_ram = None
        gc.collect()
        ram = RamVolume(CACHE_SIZE)
        memmove(ram.data, cache_bytes, len(cache_bytes))
        self.drives = [VolatileVolume(CORE_BLOCKS * BLK, uuid=u) for u in self.uuids]
        for d, b in zip(self.drives, durables):
            d.durable = bytearray(b)
            d.fua_flag = self.fua_flag
        self.cache = Cache.load_from_device(ram)
        self.queue = self.cache.get_default_queue()
        self.vols = []
        for i in range(len(self.drives)):
            v = CoreVolume(self.cache.get_core_by_name("core%d" % i))
            v.open()
            self.vols.append(v)
        self.ram = ram

    def finish(self):
        for v in self.vols:
            v.close()
        self.cache.stop()


def check_after_crash(r, blk, last_flushed, newer, older=ZERO, core=0):
    got = r.read(blk, core)
    if got == last_flushed or (newer is not None and got == newer):
        return
    what = "older (stale)" if got == older else "unexpected"
    pytest.fail("block %d after crash: %s content %r" % (blk, what, got[:16]))


@pytest.mark.parametrize("flush_before", [False, True])
def test_cache_mode_wb_to_wt(pyocf_ctx, flush_before):
    """D1 dirty in WB, then WT overwrites it, crash before any core flush"""
    r = Rig(pyocf_ctx, CacheMode.WB)
    d1 = r.write(0)
    r.bflush()
    if flush_before:
        r.cache.flush()
    r.cache.change_cache_mode(CacheMode.WT)
    d2 = r.write(0)
    if not flush_before:
        assert r.dirty_lines() == 1  # WT write leaves the dirty line dirty
    r.crash()
    check_after_crash(r, 0, d1, d2)
    r.finish()


def test_cache_mode_wb_to_wt_flush_core_after(pyocf_ctx):
    """Control: D2 flushed by the user must read back as D2"""
    r = Rig(pyocf_ctx, CacheMode.WB)
    r.write(0)
    r.bflush()
    r.cache.change_cache_mode(CacheMode.WT)
    d2 = r.write(0)
    r.bflush()
    r.crash()
    check_after_crash(r, 0, d2, None)
    r.finish()


def test_cache_mode_wb_to_wt_mngt_flush_after(pyocf_ctx):
    """Management flush after the WT write, then crash"""
    r = Rig(pyocf_ctx, CacheMode.WB)
    d1 = r.write(0)
    r.bflush()
    r.cache.change_cache_mode(CacheMode.WT)
    d2 = r.write(0)
    r.cache.flush()
    assert r.dirty_lines() == 0
    r.crash()
    check_after_crash(r, 0, d1, d2)
    r.finish()


@pytest.mark.parametrize("new", [CacheMode.WT, CacheMode.WA, CacheMode.PT, CacheMode.WI])
@pytest.mark.parametrize("flush_before", [False, True])
def test_class_mode_from_wb(pyocf_ctx, new, flush_before):
    """io class mode changes from WB, no flush on the way (PT/WI: controls)"""
    r = Rig(pyocf_ctx, CacheMode.WB, cls1=CacheMode.WB)
    d1 = r.write(0, io_class=1)
    r.bflush()
    if flush_before:
        r.cache.flush()
    r.set_class(1, new)
    d2 = r.write(0, io_class=1)
    r.crash()
    check_after_crash(r, 0, d1, d2)
    r.finish()


def test_class_wb_line_overwritten_by_wt_class(pyocf_ctx):
    """Line dirty in a WB class, overwritten by a request of a WT class"""
    r = Rig(pyocf_ctx, CacheMode.WB, cls1=CacheMode.WB)
    d1 = r.write(0, io_class=1)
    r.bflush()
    r.set_class(2, CacheMode.WT)
    d2 = r.write(0, io_class=2)
    assert r.dirty_lines() == 1
    # the dirty line moved with the request to the class of the writer
    assert r.cache.get_ioclass_stats(1)["usage"]["dirty"]["value"] == 0
    assert r.cache.get_ioclass_stats(2)["usage"]["dirty"]["value"] == 1
    r.crash()
    check_after_crash(r, 0, d1, d2)
    r.finish()


@pytest.mark.parametrize("target", ["core", "cache"])
def test_wb_write_during_mngt_flush(pyocf_ctx, target):
    """
    Management flush (of core0, or of the whole cache) in progress, held on
    core0's drive: the dirty counter is frozen and WB writes are served as WT.
    A line of core1 that is dirty gets overwritten. With target=cache the
    flush may also clean core1's line first, then there is nothing to lose.
    """
    r = Rig(pyocf_ctx, CacheMode.WB, ncores=2)
    for b in range(8):
        r.write(b, core=0)
    d1 = r.write(0, core=1)
    r.bflush(core=1)
    r.drives[0].hold_write = lambda k, a, n: True
    t = threading.Thread(target=r.cores[0].flush if target == "core" else r.cache.flush)
    t.start()
    deadline = time.time() + 10
    while not r.drives[0].held:
        assert time.time() < deadline
        time.sleep(0.001)
    d2 = r.write(0, core=1)
    r.drives[0].hold_write = None
    r.drives[0].release_writes()
    t.join(30)
    assert not t.is_alive()
    r.crash()
    check_after_crash(r, 0, d1, d2, core=1)
    r.finish()


def test_mngt_flush_converges_under_writes(pyocf_ctx):
    """Management flush with WB writes running must end and leave nothing dirty"""
    r = Rig(pyocf_ctx, CacheMode.WB)
    for b in range(64):
        r.write(b)
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                r.write(i % 64)
            except Exception as e:  # noqa
                errors.append(e)
                return
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    start = time.time()
    r.cache.flush()
    took = time.time() - start
    stop.set()
    t.join(30)
    assert not errors
    assert took < 30
    r.cache.flush()
    assert r.cache.get_stats()["usage"]["dirty"]["value"] == 0
    r.finish()


def test_partly_dirty_line_wt_overwrite(pyocf_ctx):
    """64k line: block 0 dirty, WT write over blocks 0-1, crash"""
    r = Rig(pyocf_ctx, CacheMode.WB, line_size=CacheLineSize.LINE_64KiB)
    d1 = r.write(0)
    r.bflush()
    r.cache.change_cache_mode(CacheMode.WT)
    v = r.write(0, nblk=2)
    r.crash()
    check_after_crash(r, 0, d1, v[0])
    check_after_crash(r, 1, ZERO, v[1])
    r.finish()


# Error paths of the WT engine over dirty lines

def prepare_dirty_then_wt(ctx, line_size=CacheLineSize.DEFAULT):
    r = Rig(ctx, CacheMode.WB, line_size=line_size)
    d1 = r.write(0)
    r.bflush()
    r.cache.change_cache_mode(CacheMode.WT)
    return r, d1


def test_cache_write_error_over_dirty(pyocf_ctx):
    """
    Cache write fails, core write succeeds (volatile). The dirty line must not
    be purged; the request is failed because the cache copy is now unreliable.
    """
    r, d1 = prepare_dirty_then_wt(pyocf_ctx)
    r.cache_ram.fail_write = lambda a, n: True
    d2 = r.write(0)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err != 0
    assert r.read(0) in (d1, d2)
    assert r.dirty_lines() == 1
    r.persist_metadata()
    r.crash()
    check_after_crash(r, 0, d1, None)
    r.finish()


def test_core_write_error_over_dirty(pyocf_ctx):
    """Core write fails: the dirty line must stay, holding the new data"""
    r, d1 = prepare_dirty_then_wt(pyocf_ctx)
    r.drives[0].fail_write = lambda k, a, n: True
    d2 = r.write(0)
    err = r.last_err
    r.drives[0].fail_write = None
    assert err != 0
    assert r.read(0) in (d1, d2)
    assert r.dirty_lines() == 1
    r.persist_metadata()
    r.crash()
    check_after_crash(r, 0, d1, d2)
    r.finish()


def test_cache_write_error_clean_line_is_purged(pyocf_ctx):
    """Clean line, cache write fails: acknowledged as before, line dropped"""
    r = Rig(pyocf_ctx, CacheMode.WT)
    r.write(0)
    r.bflush()
    r.cache_ram.fail_write = lambda a, n: True
    d2 = r.write(0)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err == 0
    assert r.read(0) == d2
    r.finish()


def test_cache_write_error_partly_dirty_line(pyocf_ctx):
    """64k line, block 0 dirty, block 1 clean: only block 1 is dropped on a cache error"""
    r, d1 = prepare_dirty_then_wt(pyocf_ctx, CacheLineSize.LINE_64KiB)
    first = r.write(0, nblk=2)  # block 1 becomes valid and clean in the cache
    r.bflush()
    r.cache_ram.fail_write = lambda a, n: True
    v = r.write(0, nblk=2)
    r.cache_ram.fail_write = None
    assert r.read(1) == v[1]
    r.persist_metadata()
    r.crash()
    check_after_crash(r, 1, first[1], v[1])
    r.finish()


# Error path of the WB engine

@pytest.mark.parametrize("variant", ["live", "clean_stop", "power_cut"])
def test_wb_cache_write_error_over_dirty(pyocf_ctx, variant):
    """
    WB write D2 over a dirty D1 fails on the cache device: the range may read
    D1 or D2 afterwards, but never the older content of the core (D0).
    live: read right after the error; clean_stop: stop and load, then read;
    power_cut: as clean_stop but crash, after a neighbouring WB write forced
    the metadata flush.
    """
    r = Rig(pyocf_ctx, CacheMode.WB)
    d0 = r.write(0)
    r.cache.flush()  # D0 durable on the core
    d1 = r.write(0)
    r.bflush()
    r.cache_ram.fail_write = lambda a, n: True
    d2 = r.write(0)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err != 0
    if variant == "live":
        assert r.dirty_lines() == 1
        got = r.read(0)
    else:
        if variant == "power_cut":
            r.persist_metadata(back=CacheMode.WB)
        r.crash(clean_stop=(variant == "clean_stop"))
        got = r.read(0)
    assert got != d0, "rolled back to the older core content"
    assert got in (d1, d2)
    r.finish()


def test_wb_cache_write_error_no_prior_dirty_is_purged(pyocf_ctx):
    """Control: no dirty data in the range, the failed write is dropped from the cache"""
    r = Rig(pyocf_ctx, CacheMode.WB)
    d0 = r.write(0)
    r.cache.flush()
    r.cache_ram.fail_write = lambda a, n: True
    r.write(0)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err != 0
    assert r.dirty_lines() == 0
    assert r.read(0) == d0
    r.finish()


def stats(r):
    """(dirty, occupancy) of core0 and of io class 0, in 4k blocks"""
    core = r.cores[0].get_stats()["usage"]
    cls = r.cache.get_ioclass_stats(0)["usage"]
    return ((core["dirty"]["value"], core["occupancy"]["value"]),
            (cls["dirty"]["value"], cls["occupancy"]["value"]))


def durable_blocks(r, first, last):
    d = bytes(r.drives[0].durable)
    return [d[b * BLK:(b + 1) * BLK] for b in range(first, last + 1)]


def test_wb_cache_write_error_mixed_line(pyocf_ctx):
    """
    64k line: the failed request covers blocks 2-9 = clean (2-4), dirty (5-6),
    clean (7-9). Only the clean runs are dropped; the line stays with its dirty
    blocks, counters stay consistent and a flush writes D1 to the core.
    """
    r = Rig(pyocf_ctx, CacheMode.WB, line_size=CacheLineSize.LINE_64KiB)
    d0 = r.write(0, nblk=16)
    r.cache.flush()
    assert stats(r) == ((0, 16), (0, 16))
    d1 = r.write(5, nblk=2)
    r.bflush()
    before = stats(r)
    assert before[0][0] > 0 and before[1][0] > 0
    r.cache_ram.fail_write = lambda a, n: True
    r.write(2, nblk=8)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err != 0
    # the line and its dirty blocks stay, counters of core and class agree
    assert stats(r)[0][0] == before[0][0] and stats(r)[1][0] == before[1][0]
    assert 0 < stats(r)[0][1] <= before[0][1] and stats(r)[0][1] == stats(r)[1][1]
    for b in range(2, 10):
        got = r.read(b)
        assert got in (d0[b], d1[b - 5] if b in (5, 6) else d0[b])
    r.cache.flush()
    assert stats(r)[0][0] == 0 and stats(r)[1][0] == 0
    got = durable_blocks(r, 0, 15)
    for b in range(16):
        want = d1[b - 5] if b in (5, 6) else d0[b]
        assert got[b] == want, "core block %d" % b
    r.finish()


def test_wb_cache_write_error_all_dirty_range(pyocf_ctx):
    """Whole request range dirty: nothing is purged, flush writes D1"""
    r = Rig(pyocf_ctx, CacheMode.WB, line_size=CacheLineSize.LINE_64KiB)
    d1 = r.write(0, nblk=16)
    r.bflush()
    before = stats(r)
    assert before == ((16, 16), (16, 16))
    r.cache_ram.fail_write = lambda a, n: True
    r.write(2, nblk=8)
    err = r.last_err
    r.cache_ram.fail_write = None
    assert err != 0
    assert stats(r) == before
    r.cache.flush()
    assert stats(r)[0][0] == 0
    assert durable_blocks(r, 0, 15) == d1
    r.finish()
