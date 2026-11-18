#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
Core flush skipping: a flush is not forwarded to a core when no write/discard
completed on that core since its last successful flush.

Crash model: a core is a drive with a volatile write cache (VolatileVolume);
the cache device is persistent. At every flush acknowledged on the exported
volume (especially skipped ones) the core is rolled back to its last completed
drive flush, the cache is kept as it stood, the cache is loaded and everything
acknowledged before the flush was issued must read back.
"""

import os
import random
import time
from ctypes import c_void_p, c_uint32
import re
import threading
from pathlib import Path

import pytest

from pyocf.ocf import OcfLib
from pyocf.types.cache import CacheMode
from pyocf.types.io import IoDir
from pyocf.types.shared import SeqCutOffPolicy, OcfError

from .harness import Harness, BLK

lib = OcfLib.getInstance()

ALL_MODES = [CacheMode.WT, CacheMode.WB, CacheMode.WA, CacheMode.PT, CacheMode.WI, CacheMode.WO]
DIRECT_MODES = [CacheMode.PT, CacheMode.WI]


def forwarded_skipped(h):
    return h.counters()


# ----------------------------------------------------------------------------
# Basic semantics
# ----------------------------------------------------------------------------


def test_switch_default_off_and_counters(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT, flush_skip=False)
    assert h.cache.get_flush_skip() == (False, 0, 0)

    h.write(1)
    h.flush()
    h.flush()
    # switch off: everything is forwarded
    assert h.cache.get_flush_skip() == (False, 2, 0)
    assert h.drive.flush_arrived == 2

    h.cache.set_flush_skip(True)
    h.flush()  # nothing written since the last successful flush
    assert h.cache.get_flush_skip() == (True, 2, 1)
    assert h.drive.flush_arrived == 2

    h.write(2)
    h.flush()
    h.flush()
    assert h.cache.get_flush_skip() == (True, 3, 2)
    assert h.drive.flush_arrived == 3

    h.verify_images()


def test_first_flush_forwarded_then_skipped_in_wb(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    for i in range(8):
        h.write(i)
    h.flush()
    h.flush()
    h.write(3)
    h.flush()
    # WB writes never reach the core: only the first flush (cold start) is forwarded
    assert h.counters() == (1, 2)
    assert h.drive.flush_arrived == 1
    h.verify_images()


@pytest.mark.parametrize("mode", [CacheMode.WT, CacheMode.WI, CacheMode.PT, CacheMode.WA])
def test_direct_core_write_rearms_flush(pyocf_ctx, mode):
    """A flush after a write that reached the core is forwarded"""
    h = Harness(pyocf_ctx, cache_mode=mode)
    h.flush()
    assert h.counters() == (1, 0)
    h.flush()
    assert h.counters() == (1, 1)

    h.write(5)
    h.flush()
    h.flush()
    assert h.drive.writes_completed >= 1, "write did not reach the core"
    assert h.counters() == (2, 2)
    h.verify_images()


# ----------------------------------------------------------------------------
# Ordering
# ----------------------------------------------------------------------------


def test_write_completing_after_flush_issued(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.flush()  # cold start flush: now flushed == done
    h.drive.hold_write = lambda *a: True
    w = h.write(7, wait=False)
    h.until(lambda: h.drive.held)
    h.flush()  # write not completed: nothing to flush yet, skipped
    assert h.counters() == (1, 1)
    assert not w.done
    h.release_writes()
    w.wait()
    f2 = h.flush()  # the late write must re-arm the flush
    assert h.counters() == (2, 1)
    assert f2.ack_t > w.ack_t
    h.verify_images()


def test_write_completing_while_core_flush_in_flight(pyocf_ctx):
    """Single boolean flag cleared on flush completion loses this write"""
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.write(1)
    h.drive.hold_flush = True
    f1 = h.flush(wait=False)  # core flush stuck at the drive
    h.until(lambda: len(h.drive.held_flushes) == 1)
    h.write(2)  # completes while f1 is in flight; not covered by f1
    h.drive.hold_flush = False
    h.release_flushes()
    f1.wait()
    h.flush()
    assert h.counters()[0] == 2, "second flush must be forwarded"
    h.verify_images()


def test_overlapping_flushes(pyocf_ctx):
    """A second flush must not be acknowledged before the first one is durable"""
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.write(1)
    h.drive.hold_flush = True
    f1 = h.flush(wait=False)
    h.until(lambda: len(h.drive.held_flushes) == 1)
    f2 = h.flush(wait=False)
    h.until(lambda: len(h.drive.held_flushes) == 2)
    assert not f1.done and not f2.done
    h.drive.hold_flush = False
    h.release_flushes()
    f1.wait()
    f2.wait()
    h.flush()
    h.verify_images()


def test_overlapping_flushes_complete_in_reverse(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.write(1)
    h.drive.hold_flush = True
    f1 = h.flush(wait=False)
    h.until(lambda: len(h.drive.held_flushes) == 1)
    h.write(2)
    f2 = h.flush(wait=False)
    h.until(lambda: len(h.drive.held_flushes) == 2)
    h.drive.hold_flush = False
    # complete the later flush first
    token, batch, fail = h.drive.held_flushes.pop(1)
    h.drive._complete_flush(token, batch, fail)
    f2.wait()
    h.release_flushes()
    f1.wait()
    assert h.counters() == (2, 0)
    h.flush()
    # the later flush made everything durable: the earlier one finishing afterwards
    # must not roll the tracking back (that would only cost a flush, never data)
    assert h.counters() == (2, 1)
    h.verify_images()


def test_failed_core_flush_then_flush(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.flush()
    h.write(4)
    h.drive.fail_flushes = 1
    f1 = h.flush()
    assert f1.error != 0
    f2 = h.flush()  # must not be skipped: the failed flush did not make W durable
    assert f2.error == 0
    assert h.counters() == (3, 0)
    h.verify_images()


def test_failed_core_flush_error_device(pyocf_ctx):
    """Same with ErrorDevice: its 2nd write-type request (the flush) fails once"""
    h = Harness(
        pyocf_ctx,
        cache_mode=CacheMode.PT,
        error_core=dict(error_seq_no={IoDir.WRITE: 1, IoDir.READ: -1}, error_max_count=1),
    )
    h.write(4)
    f1 = h.flush()
    assert f1.error != 0
    f2 = h.flush()
    assert f2.error == 0
    assert h.counters() == (2, 0)
    assert h.drive.flush_arrived == 1  # the failed one never reached the drive
    h.verify_images()


# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES)
def test_modes_mixed_ops(pyocf_ctx, mode):
    h = Harness(pyocf_ctx, cache_mode=mode)
    rng = random.Random(int(mode))
    for _ in range(40):
        r = rng.random()
        if r < 0.6:
            h.write(rng.randrange(0, 200), rng.randrange(1, 5))
        elif r < 0.7:
            h.discard(rng.randrange(0, 200), rng.randrange(1, 5))
        elif r < 0.8:
            h.read(rng.randrange(0, 200), rng.randrange(1, 5))
        else:
            h.flush()
    h.flush()
    fwd, skp = h.counters()
    assert fwd == h.drive.flush_arrived
    assert fwd + skp == len([o for o in h.ops if o.kind == "f"])
    h.verify_images()


@pytest.mark.parametrize("class_mode", [CacheMode.WT, CacheMode.WB, CacheMode.PT, CacheMode.WI,
                                        CacheMode.WO, CacheMode.WA])
@pytest.mark.parametrize("cache_mode", [CacheMode.WB, CacheMode.WT])
def test_per_io_class_modes(pyocf_ctx, cache_mode, class_mode):
    h = Harness(pyocf_ctx, cache_mode=cache_mode)
    h.set_class_mode(1, class_mode)
    h.set_class_mode(2, CacheMode.PT)
    rng = random.Random(1)
    for _ in range(40):
        r = rng.random()
        if r < 0.7:
            h.write(rng.randrange(0, 100), rng.randrange(1, 4), io_class=rng.choice([0, 1, 2]))
        else:
            h.flush()
    h.flush()
    h.verify_images()


def test_io_class_mode_change_mid_stream(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    h.set_class_mode(1, CacheMode.WB)
    for mode in [CacheMode.WB, CacheMode.PT, CacheMode.WB, CacheMode.WI, CacheMode.WT,
                 CacheMode.WB]:
        h.set_class_mode(1, mode)
        for i in range(5):
            h.write(10 + i, io_class=1)
        h.flush()
        h.flush()
    h.verify_images()


def test_cache_mode_change_mid_stream(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    for mode in [CacheMode.WB, CacheMode.PT, CacheMode.WB, CacheMode.WT, CacheMode.WI,
                 CacheMode.WO, CacheMode.WB]:
        h.cache.change_cache_mode(mode)
        for i in range(5):
            h.write(20 + i)
        h.flush()
        h.flush()
    h.verify_images()


@pytest.mark.parametrize("mode", [CacheMode.WB, CacheMode.WT])
def test_sequential_cutoff(pyocf_ctx, mode):
    h = Harness(pyocf_ctx, cache_mode=mode, seq_cutoff=(SeqCutOffPolicy.ALWAYS, 4 * BLK))
    h.flush()
    before = h.drive.writes_completed
    blk = 0
    for _ in range(12):  # one stream, beyond the threshold: pass-through
        h.write(blk, 2)
        blk += 2
    h.flush()
    h.flush()
    if mode == CacheMode.WB:
        assert h.drive.writes_completed > before, "sequential cutoff did not hit the core"
        assert h.counters() == (2, 1)
    h.verify_images()


def test_cache_full_fallback(pyocf_ctx):
    """WB cache without free or cleanable lines hands writes to the core"""
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB, core_blocks=4096)
    h.flush()
    rng = random.Random(3)
    blocks = rng.sample(range(4096), 3500)
    for b in blocks:
        h.write(b)
        if h.drive.writes_completed:
            break
    assert h.drive.writes_completed, "cache never filled up"
    for b in blocks[:20]:
        h.write(b)
    h.flush()
    h.flush()
    assert h.counters()[0] >= 2
    h.verify_images()


def test_fallback_pt_after_cache_errors(pyocf_ctx):
    h = Harness(
        pyocf_ctx,
        cache_mode=CacheMode.WT,
        error_cache=dict(error_seq_no={IoDir.READ: 0, IoDir.WRITE: -1}, armed=False),
    )
    lib.ocf_mngt_cache_set_fallback_pt_error_threshold.argtypes = [c_void_p, c_uint32]
    lib.ocf_mngt_cache_set_fallback_pt_error_threshold(h.cache.cache_handle, 1)
    for i in range(10):
        h.write(i)
    h.flush()
    h.cache_dev.arm()
    for i in range(10):  # read hits now fail on the cache device
        try:
            h.read(i)
        except AssertionError:
            pass
    h.cache_dev.disarm()
    before = h.drive.writes_completed
    for i in range(10, 20):
        h.write(i)
    assert h.drive.writes_completed > before
    h.flush()
    h.flush()
    h.verify_images()


@pytest.mark.parametrize("mode", [CacheMode.WB, CacheMode.WT, CacheMode.PT])
def test_discard(pyocf_ctx, mode):
    """A discard is a core write: the flush after it must reach the core"""
    h = Harness(pyocf_ctx, cache_mode=mode)
    for i in range(10):
        h.write(i)
    h.flush()
    h.cache.flush() if mode == CacheMode.WB else None
    h.flush()  # align: everything durable, flush skippable
    h.discard(2, 3)
    h.flush()
    h.verify_images()


def test_discard_rearms_flush_in_pt(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.write(1)
    h.flush()
    h.flush()
    assert h.counters() == (1, 1)
    h.discard(1)
    h.flush()
    assert h.counters() == (2, 1)
    h.verify_images()


def test_detach_attach(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    for i in range(10):
        h.write(i)
    h.flush()
    h.detach()
    h.flush()
    for i in range(10, 15):  # d2c writes
        h.write(i)
    h.flush()
    h.flush()
    h.attach()
    for i in range(15, 20):
        h.write(i)
    h.flush()
    h.write(2)
    h.flush()
    h.verify_images()


def test_cleaner_exempt(pyocf_ctx):
    """Cleaner core writes are not counted: the flush after them is skipped"""
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    for i in range(20):
        h.write(i)
    h.flush()  # cold start flush
    h.cache.flush()  # cleaner writes dirty lines to the core and flushes it
    assert h.drive.writes_completed > 0
    assert h.drive.flush_completed >= 1
    for i in range(20):
        assert h.drive.durable[i * BLK:(i + 1) * BLK] != bytes(BLK)
    h.flush()
    assert h.counters() == (1, 1)
    h.verify_images()


def test_cleaner_concurrent(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB, cleaner=True)
    stop = threading.Event()

    def cleaning():
        while not stop.is_set():
            with h.quiesce:
                h.cache.flush()

    t = threading.Thread(target=cleaning)
    t.start()
    try:
        rng = random.Random(5)
        for _ in range(60):
            if rng.random() < 0.7:
                h.write(rng.randrange(0, 40), 1)
            else:
                h.flush()
        h.flush()
    finally:
        stop.set()
        t.join()
    h.verify_images()


# ----------------------------------------------------------------------------
# Transitions
# ----------------------------------------------------------------------------


def test_switch_defaults_off_after_load(pyocf_ctx):
    from pyocf.types.cache import Cache

    h = Harness(pyocf_ctx, cache_mode=CacheMode.WB)
    assert h.cache.get_flush_skip()[0]
    h.write(1)
    h.vol.close()
    h.cache.stop()
    cache = Cache.load_from_device(h.cache_dev)
    assert cache.get_flush_skip() == (False, 0, 0)
    core = cache.get_core_by_name(h.core_name)
    cache.stop()
    del core


def test_first_flush_after_load_forwarded(pyocf_ctx):
    from pyocf.types.cache import Cache
    from pyocf.types.volume_core import CoreVolume

    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.write(1)
    h.flush()
    h.vol.close()
    h.cache.stop()
    h.vol = None

    cache = Cache.load_from_device(h.cache_dev)
    cache.set_flush_skip(True)
    core = cache.get_core_by_name(h.core_name)
    vol = CoreVolume(core)
    vol.open()
    queue = cache.get_default_queue()
    from pyocf.types.io import Sync
    for _ in range(2):
        io = vol.new_io(queue, 0, 0, IoDir.WRITE, 0, 0)
        assert int(Sync(io).submit_flush().results["err"]) == 0
    assert cache.get_flush_skip() == (True, 1, 1)
    vol.close()
    cache.stop()


def test_first_flush_after_core_add_forwarded(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT)
    h.flush()
    h.flush()
    assert h.counters() == (1, 1)
    h.vol.close()
    h.cache.remove_core(h.core)
    h.cache.add_core(h.core)
    from pyocf.types.volume_core import CoreVolume
    h.vol = CoreVolume(h.core)
    h.vol.open()
    h.flush()
    h.flush()
    assert h.counters() == (2, 2)


def test_switch_on_with_direct_writes_pending(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT, flush_skip=False)
    h.flush()
    h.write(3)  # direct write, never flushed
    h.cache.set_flush_skip(True)
    h.flush()
    assert h.counters() == (2, 0)
    h.flush()
    assert h.counters() == (2, 1)
    h.cache.set_flush_skip(False)
    h.write(4)
    h.cache.set_flush_skip(True)
    h.flush()
    assert h.counters() == (3, 1)
    h.verify_images()


def test_switch_off_flush_advances_tracking(pyocf_ctx):
    h = Harness(pyocf_ctx, cache_mode=CacheMode.PT, flush_skip=False)
    h.write(3)
    h.flush()  # forwarded while off
    h.cache.set_flush_skip(True)
    h.flush()  # nothing since: skipped
    assert h.counters() == (1, 1)
    h.verify_images()


# ----------------------------------------------------------------------------
# Counting contract: every core write path is known
# ----------------------------------------------------------------------------

ALLOWED_CALLERS = {
    # counted core writes/discards on behalf of user requests
    "ocf_engine_forward_core_io_req": {
        "engine/engine_d2c.c", "engine/engine_pt.c", "engine/engine_rd.c",
        "engine/engine_wi.c", "engine/engine_wo.c", "engine/engine_wt.c",
    },
    "ocf_engine_forward_core_discard_req": {"engine/engine_d2c.c", "engine/engine_discard.c"},
    "ocf_engine_forward_core_flush_req": {"engine/engine_d2c.c", "engine/engine_flush.c"},
    # exempt: cleaner (requests have req->cleaner set)
    "ocf_req_forward_core_io": {"engine/engine_io.c", "utils/utils_cleaner.c"},
    "ocf_req_forward_core_discard": {"engine/engine_io.c"},
    "ocf_req_forward_core_flush": {"engine/engine_io.c", "utils/utils_cleaner.c"},
    # volume composition, forwards to members with the caller's token
    "ocf_forward_io": {"ocf_composite_volume.c"},
    "ocf_forward_discard": {"ocf_composite_volume.c"},
    "ocf_forward_flush": {"ocf_composite_volume.c"},
}


def test_core_forward_callers_are_known():
    """
    A new caller of a core forward function must be reviewed for flush skipping:
    it either counts (default for ocf_req_forward_core_io/discard) or needs to
    be exempted explicitly.
    """
    src = Path(__file__).resolve().parents[4] / "src"
    assert src.is_dir(), src
    found = {}
    pat = re.compile(r"\b(" + "|".join(ALLOWED_CALLERS) + r")\s*\(")
    for path in src.rglob("*.c"):
        rel = path.relative_to(src).as_posix()
        text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                      path.read_text(), flags=re.S)
        for m in pat.finditer(text):
            name = m.group(1)
            line = text[:m.start()].rsplit("\n", 1)[-1]
            if re.match(r"^(void|int|static)\b", line):
                continue  # a definition
            found.setdefault(name, set()).add(rel)
    for name, files in found.items():
        unknown = files - ALLOWED_CALLERS[name]
        assert not unknown, "unreviewed caller of %s in %s" % (name, sorted(unknown))


# ----------------------------------------------------------------------------
# Randomised sequences
# ----------------------------------------------------------------------------

ITERATIONS = int(os.environ.get("FLUSH_SKIP_ITERATIONS", "12"))


def flush_ok(h):
    """
    Flush the cache before a mode change, as `casadm -Q -f` does. Returns False
    when the flush failed (injected core flush error); the change is skipped then.
    """
    try:
        h.cache.flush()
        return True
    except OcfError:
        return False


def random_sequence(ctx, seed, switch=True, failures=True):
    rng = random.Random(seed)
    mode = rng.choice(ALL_MODES)
    h = Harness(
        ctx,
        cache_mode=mode,
        seq_cutoff=rng.choice([None, (SeqCutOffPolicy.ALWAYS, 8 * BLK)]),
        flush_skip=switch,
    )
    h.set_class_mode(1, rng.choice(ALL_MODES))
    outstanding = []
    n_ops = rng.randrange(20, 50)

    def busy_blocks():
        return {b for op in outstanding if not op.done and op.kind != "f"
                for b in range(op.blk, op.blk + op.nblk)}

    def quiet():
        return not any(not op.done for op in outstanding)

    def settle_held(op, what):
        deadline = time.time() + 10
        while not op.done and not what() and time.time() < deadline:
            time.sleep(0.001)

    def held_write(blk, nblk, io_class):
        fired = []

        def hold(kind, addr, nbytes):
            if fired:
                return False
            fired.append(1)
            return True

        h.drive.hold_write = hold
        op = h.write(blk, nblk, io_class=io_class, wait=False)
        outstanding.append(op)
        settle_held(op, lambda: h.drive.held)
        h.drive.hold_write = None

    def held_flush():
        h.drive.hold_flush = True
        op = h.flush(wait=False)
        outstanding.append(op)
        settle_held(op, lambda: h.drive.held_flushes)
        h.drive.hold_flush = False

    for _ in range(n_ops):
        r = rng.random()
        busy = busy_blocks()
        blk, nblk = rng.randrange(0, 120), rng.randrange(1, 6)
        free = not any(b in busy for b in range(blk, blk + nblk))
        if r < 0.45:
            if not free:
                continue
            io_class = rng.choice([0, 0, 1])
            if rng.random() < 0.25:
                held_write(blk, nblk, io_class)
            else:
                outstanding.append(h.write(blk, nblk, io_class=io_class))
        elif r < 0.52:
            if free:
                outstanding.append(h.discard(blk, nblk))
        elif r < 0.65:
            if rng.random() < 0.3:
                held_flush()
            else:
                outstanding.append(h.flush())
        elif r < 0.72:
            h.release_writes(rng.choice([None, 1]))
            h.log("release writes")
        elif r < 0.78:
            h.release_flushes(rng.choice([None, 1]))
            h.log("release flushes")
        elif r < 0.82:
            if quiet():
                m = rng.choice(ALL_MODES)
                h.log("cache mode %s" % m.name)
                if flush_ok(h):  # as casadm -Q -f
                    h.cache.change_cache_mode(m)
        elif r < 0.86:
            if quiet():
                m = rng.choice(ALL_MODES)
                h.log("class1 mode %s" % m.name)
                if flush_ok(h):
                    h.set_class_mode(1, m)
        elif r < 0.9:
            on = rng.random() < 0.8
            h.log("switch %s" % (switch and on))
            h.cache.set_flush_skip(switch and on)
        elif r < 0.95:
            if failures:
                h.drive.fail_flushes = 1
                h.log("fail next core flush")
        elif quiet():  # a read settles the cache, which held requests prevent
            h.read_any(blk, nblk)
    h.release_all()
    for op in outstanding:
        op.wait()
    h.drive.fail_flushes = 0
    h.cache.set_flush_skip(switch)
    h.flush()
    h.flush()
    return h


@pytest.mark.parametrize("seed", range(ITERATIONS))
def test_random_sequences(pyocf_ctx, seed):
    """
    The sequence must not violate the oracle, with the switch off (every flush is
    forwarded) and on.
    """
    def run(switch):
        random_sequence(pyocf_ctx, seed, switch=switch).verify_images(seed=seed)

    run(False)
    run(True)
