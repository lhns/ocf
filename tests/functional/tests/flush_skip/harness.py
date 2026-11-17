#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
Test harness for core flush skipping.

VolatileVolume models a drive with a volatile write cache: a write completed
to OCF is only durable after a flush completed. Harness drives a cache over
such a core, tracks every operation acknowledged on the exported volume and,
at each flush acknowledged on it, records a crash image (cache device as it
stood, core as it was after its last completed drive flush). Images are
verified afterwards by loading the cache and reading back.
"""

import gc
import os
import random
import threading
import time
from ctypes import cast, c_void_p, memmove, memset, string_at

from pyocf.ocf import OcfLib
from pyocf.types.cache import Cache, CacheMode, CleaningPolicy
from pyocf.types.core import Core
from pyocf.types.data import Data
from pyocf.types.io import Io, IoDir, Sync
from pyocf.types.volume import Volume, RamVolume, ErrorDevice
from pyocf.types.volume_core import CoreVolume
from pyocf.types.shared import OcfErrorCode
from pyocf.utils import Size

BLK = 4096
ZERO = bytes(BLK)
WAIT_TIMEOUT = 30

lib = OcfLib.getInstance()

ACTIVE = []  # harnesses with a running cache, see conftest.py


def pattern(seq, blk):
    """Self-identifying 4k block content"""
    return (b"%08x%08x" % (seq, blk)) * (BLK // 16)


class VolatileVolume(Volume):
    """
    Drive with a volatile write cache.

    durable - what survives a power cut
    pending - completed (acknowledged) writes not yet covered by a flush
    A flush covers writes completed before it ARRIVED at the drive; writes
    completing while it is in flight stay pending. Held writes are not
    completed and therefore not visible to reads or flushes.
    """

    def __init__(self, size_bytes, uuid=None):
        super().__init__(uuid)
        self.size = size_bytes
        self.durable = bytearray(size_bytes)
        self.pending = []  # (id, ("w", addr, bytes) | ("z", addr, nbytes))
        self.next_id = 1
        self.trace = []  # drive level events, for failure reports
        self.lock = threading.RLock()

        self.hold_write = None  # predicate (kind, addr, nbytes) -> bool
        self.hold_flush = False
        self.held = []  # [(token, op)]
        self.held_flushes = []  # [(token, batch, fail)]
        self.fail_flushes = 0
        self.flush_arrived = 0
        self.flush_completed = 0
        self.flush_failed = 0
        self.writes_completed = 0

    def get_length(self):
        return self.size

    def get_max_io_size(self):
        return Size.from_KiB(128)

    @staticmethod
    def apply(buf, op):
        kind, addr, arg = op
        if kind == "w":
            buf[addr:addr + len(arg)] = arg
        else:
            buf[addr:addr + arg] = bytes(arg)

    def view(self, addr, nbytes):
        """Read path: durable overlaid with completed pending writes"""
        with self.lock:
            buf = bytearray(self.durable[addr:addr + nbytes])
            for _, (kind, a, arg) in self.pending:
                n = len(arg) if kind == "w" else arg
                lo, hi = max(a, addr), min(a + n, addr + nbytes)
                if lo >= hi:
                    continue
                if kind == "w":
                    buf[lo - addr:hi - addr] = arg[lo - a:hi - a]
                else:
                    buf[lo - addr:hi - addr] = bytes(hi - lo)
            return bytes(buf)

    def crash_image(self, rng=None):
        """
        Core content after a power cut now. rng None: everything unflushed is
        lost. Otherwise a random subset of the pending writes survives.
        """
        with self.lock:
            buf = bytearray(self.durable)
            if rng is not None:
                for _, op in self.pending:
                    if rng.random() < 0.5:
                        self.apply(buf, op)
            return bytes(buf)

    def power_cut(self, rng=None):
        with self.lock:
            self.durable = bytearray(self.crash_image(rng))
            self.pending = []

    # forward ops
    def do_forward_io(self, token, rw, addr, nbytes, offset):
        data_ptr = cast(lib.ocf_forward_get_data(token), c_void_p)
        data = Data.get_instance(data_ptr.value).handle.value + offset
        if rw == IoDir.READ:
            memmove(data, self.view(addr, nbytes), nbytes)
            Io.forward_end(token, 0)
            return

        self._write_or_hold(token, ("w", addr, string_at(data, nbytes)))

    def do_forward_discard(self, token, addr, nbytes):
        self._write_or_hold(token, ("z", addr, nbytes))

    def _write_or_hold(self, token, op):
        kind, addr, arg = op
        nbytes = len(arg) if kind == "w" else arg
        with self.lock:
            if self.hold_write and self.hold_write(kind, addr, nbytes):
                self.held.append((token, op))
                return
        self._complete_write(token, op)

    def _complete_write(self, token, op):
        with self.lock:
            self.pending.append((self.next_id, op))
            self.trace.append("drive: write id %d %s addr %d completed"
                              % (self.next_id, op[0], op[1]))
            self.next_id += 1
            self.writes_completed += 1
        Io.forward_end(token, 0)

    def release_writes(self, count=None):
        """Complete held writes (all, or the oldest count)"""
        while True:
            with self.lock:
                if not self.held or count == 0:
                    return
                token, op = self.held.pop(0)
                if count is not None:
                    count -= 1
            self._complete_write(token, op)

    def do_forward_flush(self, token):
        with self.lock:
            self.flush_arrived += 1
            batch = self.next_id - 1  # covers writes completed up to now
            self.trace.append("drive: flush arrived, covers ids <= %d" % batch)
            fail = self.fail_flushes > 0
            if fail:
                self.fail_flushes -= 1
            if self.hold_flush:
                self.held_flushes.append((token, batch, fail))
                return
        self._complete_flush(token, batch, fail)

    def _complete_flush(self, token, batch, fail):
        with self.lock:
            if fail:
                self.flush_failed += 1
            else:
                for op_id, op in self.pending:
                    if op_id <= batch:
                        self.apply(self.durable, op)
                self.pending = [(i, op) for i, op in self.pending if i > batch]
                self.flush_completed += 1
                self.trace.append("drive: flush covering <= %d completed" % batch)
        Io.forward_end(token, -OcfErrorCode.OCF_ERR_IO if fail else 0)

    def release_flushes(self, count=None):
        while True:
            with self.lock:
                if not self.held_flushes or count == 0:
                    return
                token, batch, fail = self.held_flushes.pop(0)
                if count is not None:
                    count -= 1
            self._complete_flush(token, batch, fail)

    def release_all(self):
        self.hold_write = None
        self.hold_flush = False
        self.release_writes()
        self.release_flushes()


class GuardedRam(RamVolume):
    """
    Cache device whose content can be copied between two I/Os: every I/O is
    atomic with respect to a crash image, as on a real device.
    """

    def __init__(self, size, uuid=None):
        super().__init__(size, uuid)
        self.io_lock = threading.Lock()
        self.io_count = 0

    def do_forward_io(self, token, rw, addr, nbytes, offset):
        try:
            with self.io_lock:
                self.io_count += 1
                if rw == IoDir.WRITE:
                    src_ptr = cast(lib.ocf_forward_get_data(token), c_void_p)
                    src = Data.get_instance(src_ptr.value).handle.value + offset
                    dst = self.data_ptr + addr
                else:
                    dst_ptr = cast(lib.ocf_forward_get_data(token), c_void_p)
                    dst = Data.get_instance(dst_ptr.value).handle.value + offset
                    src = self.data_ptr + addr
                memmove(dst, src, nbytes)
        except Exception:  # noqa
            Io.forward_end(token, -OcfErrorCode.OCF_ERR_IO)
            return
        Io.forward_end(token, 0)

    def do_forward_discard(self, token, addr, nbytes):
        with self.io_lock:
            memset(self.data_ptr + addr, 0, nbytes)
        Io.forward_end(token, 0)

    def snapshot(self):
        with self.io_lock:
            return string_at(self.data_ptr, int(self.size))


class _AnyContent:
    """Expected content of a discarded block: undefined, anything goes"""

    def __repr__(self):
        return "<any>"


ANY = _AnyContent()


class Op:
    def __init__(self, kind, blk, nblk, values, issue_t):
        self.kind = kind  # "w", "z", "f"
        self.blk = blk
        self.nblk = nblk
        self.values = values  # {blk: bytes} the op makes of each block
        self.issue_t = issue_t
        self.ack_t = None
        self.error = 0
        self.event = threading.Event()
        self.image = None  # flush only
        self.sync = False  # flush only: harness captures the cache once OCF settled

    def wait(self, timeout=WAIT_TIMEOUT):
        assert self.event.wait(timeout), "operation did not complete"
        return self

    @property
    def done(self):
        return self.event.is_set()


class Image:
    """Crash image taken when a flush was acknowledged on the exported volume"""

    def __init__(self, label, cache_bytes, durable, pending, expected, allowed_extra, detached):
        self.label = label
        self.detached = detached
        self.cache_bytes = cache_bytes
        self.durable = durable
        self.pending = pending
        self.expected = expected
        self.allowed_extra = allowed_extra


class Harness:
    CACHE_SIZE = Size.from_MiB(40)
    CORE_BLOCKS = 1024

    _counter = 0

    def __init__(
        self,
        ctx,
        cache_mode=CacheMode.WB,
        flush_skip=True,
        error_core=None,
        error_cache=None,
        seq_cutoff=None,
        cleaner=False,
        cache_size=None,
        core_blocks=None,
        **cache_kwargs,
    ):
        """
        error_core: kwargs for an ErrorDevice wrapped around the core drive
        error_cache: kwargs for an ErrorDevice wrapped around the cache device
        seq_cutoff: (policy, threshold) for the core
        """
        type(self)._counter += 1
        self.ctx = ctx
        self.cache_mode = cache_mode
        self.flush_skip = flush_skip
        self.cache_kwargs = cache_kwargs
        self.seq_cutoff = seq_cutoff
        self.cleaner = cleaner
        self.cache_size = cache_size or self.CACHE_SIZE
        self.core_blocks = core_blocks or self.CORE_BLOCKS
        self.detached = False
        self.core_uuid = "fs-core-%d" % type(self)._counter
        self.core_name = "core1"

        self.lock = threading.RLock()
        # background cleaning must not run while a crash image is taken
        self.quiesce = threading.Lock()
        self.clock = 0
        self.ops = []
        self.events = []
        self.images = []
        self.inflight = {}  # id(op) -> op

        for vol_type in (VolatileVolume, GuardedRam):
            if vol_type not in ctx.ocf_volume_type:
                ctx.register_volume_type(vol_type)

        self.cache_ram = GuardedRam(self.cache_size)
        if error_cache is not None:
            self.cache_dev = ErrorDevice(self.cache_ram, **error_cache)
        else:
            self.cache_dev = self.cache_ram
        if error_core is not None:
            self.drive = VolatileVolume(self.core_blocks * BLK)
            self.core_dev = ErrorDevice(self.drive, uuid=self.core_uuid, **error_core)
        else:
            self.drive = VolatileVolume(self.core_blocks * BLK, uuid=self.core_uuid)
            self.core_dev = self.drive
        self.cache = None
        self.core = None
        self.vol = None
        ACTIVE.append(self)
        self.start()

    # setup
    def start(self):
        self.cache = Cache.start_on_device(
            self.cache_dev,
            cache_mode=self.cache_mode,
            **self.cache_kwargs,
        )
        if not self.cleaner:
            # not disable_cleaner: attaching a device later crashes OCF (pre-existing)
            self.cache.set_cleaning_policy(CleaningPolicy.NOP)
        self.cache.set_flush_skip(self.flush_skip)
        self.core = Core.using_device(self.core_dev, name=self.core_name)
        self.cache.add_core(self.core)
        if self.seq_cutoff:
            policy, threshold = self.seq_cutoff
            self.core.set_seq_cut_off_policy(policy)
            self.core.set_seq_cut_off_threshold(threshold)
        self.queue = self.cache.get_default_queue()
        self.vol = CoreVolume(self.core)
        self.vol.open()

    def log(self, text):
        with self.lock:
            self.events.append((self.clock, text))

    def describe(self):
        lines = []
        for op in self.ops:
            lines.append((op.issue_t, "issue %s blk %d+%d" % (op.kind, op.blk, op.nblk)))
            if op.ack_t:
                lines.append((op.ack_t, "ack   %s blk %d+%d err=%d"
                              % (op.kind, op.blk, op.nblk, op.error)))
        lines += self.events
        text = "\n".join("%4d %s" % l for l in sorted(lines, key=lambda x: x[0]))
        return text + "\n" + "\n".join(self.drive.trace if self.drive else
                                        getattr(self, "drive_trace", []))

    def detach(self):
        self.cache.detach_device()
        self.detached = True

    def attach(self):
        self.cache.attach_device(self.cache_dev, force=True)
        self.detached = False

    @staticmethod
    def until(pred, timeout=10):
        """Wait for asynchronous OCF activity, e.g. a forward reaching a held drive"""
        deadline = time.time() + timeout
        while not pred():
            assert time.time() < deadline, "timeout waiting for condition"
            time.sleep(0.001)

    def quiesce_activity(self, rounds=4):
        """
        Wait until OCF stopped issuing I/O. Requests keep running for a while after
        they were acknowledged, and a crash image taken meanwhile would show a
        request half way through (a legitimate crash state, but not the one
        under test).
        """
        deadline = time.time() + 10
        stable, last = 0, None
        while stable < rounds:
            cur = (self.cache_ram.io_count, self.drive.writes_completed,
                   self.drive.flush_arrived, self.drive.flush_completed)
            stable = stable + 1 if cur == last else 0
            last = cur
            assert time.time() < deadline, "OCF does not quiesce"
            time.sleep(0.01)

    def release_writes(self, count=None):
        self.drive.release_writes(count)
        self.quiesce_activity()

    def release_flushes(self, count=None):
        self.quiesce_activity()
        self.drive.release_flushes(count)

    def release_all(self):
        self.drive.hold_write = None
        self.drive.hold_flush = False
        self.release_writes()
        self.until(lambda: all(op.done for op in self.ops if op.kind != "f"))
        self.release_flushes()

    def counters(self):
        """(forwarded, skipped)"""
        _, fwd, skp = self.cache.get_flush_skip()
        return fwd, skp

    def set_class_mode(self, class_id, mode, name=None, max_size=100):
        self.cache.configure_partition(
            part_id=class_id,
            name=name or "class%d" % class_id,
            max_size=max_size,
            priority=1,
            cache_mode=mode,
        )

    # operations
    def _tick(self):
        self.clock += 1
        return self.clock

    def _check_overlap(self, blk, nblk):
        for other in self.inflight.values():
            if other.kind != "f" and not (
                blk + nblk <= other.blk or other.blk + other.nblk <= blk
            ):
                raise AssertionError("overlapping in-flight operations are not supported")

    def _submit(self, op, io, submit):
        def cb(err):
            with self.lock:
                op.ack_t = self._tick()
                op.error = err
                self.inflight.pop(id(op), None)
                if op.kind == "f":
                    op.image = self._take_image(op, copy_cache=not op.sync)
            op.event.set()

        io.callback = cb
        submit(io)
        return op

    def _new_op(self, kind, blk, nblk, values):
        with self.lock:
            if kind != "f":
                self._check_overlap(blk, nblk)
            op = Op(kind, blk, nblk, values, self._tick())
            self.ops.append(op)
            self.inflight[id(op)] = op
        return op

    def write(self, blk, nblk=1, io_class=0, wait=True, flags=0):
        seq = len(self.ops) + 1
        values = {b: pattern(seq, b) for b in range(blk, blk + nblk)}
        data = Data.from_bytes(b"".join(values[b] for b in range(blk, blk + nblk)))
        op = self._new_op("w", blk, nblk, values)
        io = self.vol.new_io(self.queue, blk * BLK, nblk * BLK, IoDir.WRITE, io_class, flags)
        io.set_data(data)
        op.data = data
        self._submit(op, io, lambda i: i.submit())
        return op.wait() if wait else op

    def discard(self, blk, nblk=1, wait=True):
        values = {b: ZERO for b in range(blk, blk + nblk)}
        op = self._new_op("z", blk, nblk, values)
        io = self.vol.new_io(self.queue, blk * BLK, nblk * BLK, IoDir.WRITE, 0, 0)
        self._submit(op, io, lambda i: i.submit_discard())
        return op.wait() if wait else op

    def flush(self, wait=True):
        if wait:
            with self.quiesce:
                return self._flush(wait)
        return self._flush(wait)

    def _flush(self, wait):
        op = self._new_op("f", 0, 0, {})
        op.sync = wait
        io = self.vol.new_io(self.queue, 0, 0, IoDir.WRITE, 0, 0)
        self._submit(op, io, lambda i: i.submit_flush())
        if wait:
            op.wait()
            # background work of the request (backfill etc.) must not tear the image
            if not self.inflight:  # a settle cannot finish while requests are held
                self.cache.settle()
            op.image.cache_bytes = self._copy_cache()
        return op

    def read(self, blk, nblk=1, io_class=0):
        return self.vol.read_sync(self.queue, blk * BLK, nblk * BLK, io_class)

    def read_any(self, blk, nblk=1):
        """A read that may fail (e.g. after injected errors); returns the error"""
        data = Data(nblk * BLK)
        io = self.vol.new_io(self.queue, blk * BLK, nblk * BLK, IoDir.READ, 0, 0)
        io.set_data(data)
        err = int(Sync(io).submit().results["err"])
        self.cache.settle()
        return err

    # oracle
    def _expectations(self, issue_t, ack_t):
        """
        Per block: the value a flush issued at issue_t must have made durable
        (last operation acknowledged before it), and the other values it may
        legitimately show (operations acknowledged after the issue, still in
        flight, or failed).
        """
        must, must_t, extra = {}, {}, {}
        for op in self.ops:
            if op.kind == "f" or op.issue_t > ack_t:
                continue
            acked_before = op.ack_t is not None and op.ack_t < issue_t and not op.error
            for b, v in op.values.items():
                if op.kind == "z":
                    # a discard leaves the content undefined
                    v = ANY
                if acked_before:
                    if b not in must_t or must_t[b] < op.ack_t:
                        must[b] = v
                        must_t[b] = op.ack_t
                else:
                    extra.setdefault(b, set()).add(v)
        return must, extra

    def _copy_cache(self):
        return self.cache_ram.snapshot()

    def _take_image(self, flush_op, copy_cache):
        # called with self.lock held, from the flush completion
        must, extra = self._expectations(flush_op.issue_t, flush_op.ack_t)
        cache_bytes = self._copy_cache() if copy_cache else None
        with self.drive.lock:
            durable = bytes(self.drive.durable)
            pending = [op for _, op in self.drive.pending]
        label = "flush issued@%d acked@%d" % (flush_op.issue_t, flush_op.ack_t)
        return Image(label, cache_bytes, durable, pending, must, extra, self.detached)

    def flush_ops(self):
        return [op for op in self.ops if op.kind == "f" and op.image is not None]

    # teardown and verification
    def stop(self):
        self.release_all()
        if self.vol:
            self.vol.close()
            self.vol = None
        if self.cache:
            self.cache.stop()
            self.cache = None

    def emergency_stop(self):
        """Best effort teardown after a failed test"""
        try:
            self.drive.release_all()
        except Exception:  # noqa
            pass
        try:
            if self.vol:
                self.vol.close()
                self.vol = None
            if self.cache and self.cache.started:
                self.cache.stop()
                self.cache = None
        except Exception:  # noqa
            pass

    def verify_images(self, seed=0, modes=("drop", "random")):
        """
        Stop the live system, then for every recorded crash image: rebuild
        devices, load the cache, read back and compare. Raises AssertionError
        listing the violations. Returns the number of images checked.
        """
        self.stop()
        self.drive_trace = list(self.drive.trace)
        images = [op.image for op in self.flush_ops() if not op.error]
        touched = sorted({b for op in self.ops if op.kind != "f" for b in op.values})
        self.core_dev = self.drive = self.cache_dev = self.cache_ram = self.core = None
        gc.collect()

        rng = random.Random(seed)
        failures = []
        for img in images:
            for mode in modes:
                err = self._verify_one(img, mode, rng, touched)
                if err:
                    failures.append("%s [%s]: %s" % (img.label, mode, err))
        assert not failures, "data lost after crash:\n%s\n%s" % (
            "\n".join(failures[:10]), self.describe())
        return len(images)

    def _load(self, cache_ram, img):
        """Load with a deadline so that a hang is reported, not endured"""
        result = {}

        def run():
            try:
                result["cache"] = Cache.load_from_device(cache_ram)
            except BaseException as e:  # noqa
                result["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(60)
        if t.is_alive():
            path = "/tmp/flush_skip_hang_%d.img" % os.getpid()
            with open(path, "wb") as f:
                f.write(img.cache_bytes)
            raise AssertionError("cache load hangs (image saved to %s)" % path)
        if "error" in result:
            raise result["error"]
        return result["cache"]

    def _verify_one(self, img, mode, rng, touched):
        buf = bytearray(img.durable)
        if mode == "random":
            for op in img.pending:
                if rng.random() < 0.5:
                    VolatileVolume.apply(buf, op)

        if img.detached:
            # no cache attached: the core alone must hold the data
            for b in touched:
                got = bytes(buf[b * BLK:(b + 1) * BLK])
                want = img.expected.get(b, ZERO)
                if (got != want and want is not ANY
                        and got not in img.allowed_extra.get(b, ())
                        and ANY not in img.allowed_extra.get(b, ())):
                    return "block %d: got %r want %r" % (b, got[:16], want[:16])
            return None

        cache_ram = RamVolume(self.cache_size)
        memmove(cache_ram.data, img.cache_bytes, len(img.cache_bytes))
        drive = VolatileVolume(self.core_blocks * BLK, uuid=self.core_uuid)
        drive.durable = buf
        cache = vol = None
        try:
            cache = self._load(cache_ram, img)
            core = cache.get_core_by_name(self.core_name)
            vol = CoreVolume(core)
            vol.open()
            queue = cache.get_default_queue()
            for b in touched:
                got = vol.read_sync(queue, b * BLK, BLK)
                want = img.expected.get(b, ZERO)
                if (got != want and want is not ANY
                        and got not in img.allowed_extra.get(b, ())
                        and ANY not in img.allowed_extra.get(b, ())):
                    return "block %d: got %r want %r" % (b, got[:16], want[:16])
            return None
        finally:
            if vol:
                vol.close()
            if cache:
                cache.stop()
            del drive
            gc.collect()
