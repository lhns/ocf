#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
A write-invalidate request that waited for a cache line lock must act on the
line state at the time it got the lock, not on the state seen when it looked
the line up. A write-back write granted the lock before it may have made the
range dirty; WI has to clean that data to the core before it overwrites the
core and drops the cache copy.

Crash model as in test_wt_over_dirty.py.
"""

import threading
import time

import pytest

from pyocf.types.cache import CacheMode, CacheLineSize
from pyocf.types.io import IoDir, Sync
from pyocf.types.data import Data

from .test_wt_over_dirty import Rig, check_after_crash
from .wt_crash_devices import pattern, BLK

ZERO = bytes(BLK)

LINE_BLOCKS = 16  # 64k cache line
CLS_WB, CLS_WT, CLS_WI = 1, 2, 3


def submit_async(r, blk, nblk, io_class, seq):
    """Write in a thread; returns (thread, content, result)"""
    vals = [pattern(seq, b) for b in range(blk, blk + nblk)]
    res = {}

    def run():
        io = r.vols[0].new_io(r.queue, blk * BLK, nblk * BLK, IoDir.WRITE, io_class, 0)
        io.set_data(Data.from_bytes(b"".join(vals)))
        res["err"] = Sync(io).submit().results["err"]

    t = threading.Thread(target=run)
    t.start()
    return t, vals, res


def wait_until(cond, what):
    deadline = time.time() + 10
    while not cond():
        assert time.time() < deadline, what
        time.sleep(0.001)


@pytest.mark.parametrize("dirty_outside", [False, True])
def test_wi_after_wb_write_in_lock_queue(pyocf_ctx, dirty_outside):
    """
    Line L (64k) is clean when WI looks it up (with dirty_outside, block 15 is
    dirty: the request info counts a line dirty if any of its blocks is, so WI
    cleans the line even without the fix; control case).
    Queue on L's lock: holder H (WT write, block 5, core write held), then a WB
    write W of blocks 0-3 (D1), then a WI write of blocks 0-1 (D2). WI looked
    the line up while it was clean. Once W is acknowledged the user flushes;
    WI's core write is held until then. After the power cut the range must read
    D1 or D2, never the content from before D1 (D0); blocks 2-3 (dirty, outside
    WI's range) read D1; block 15 keeps its content.
    """
    r = Rig(pyocf_ctx, CacheMode.WB, line_size=CacheLineSize.LINE_64KiB)
    r.set_class(CLS_WB, CacheMode.WB)
    r.set_class(CLS_WT, CacheMode.WT)
    r.set_class(CLS_WI, CacheMode.WI)
    d0 = r.write(0, nblk=2, io_class=CLS_WT)  # line mapped, clean
    r.bflush()  # D0 durable
    y1 = ZERO
    if dirty_outside:
        y1 = r.write(15, io_class=CLS_WB)
        r.bflush()
        assert r.dirty_lines() > 0

    def core_hold(kind, addr, n):
        return addr // BLK == 5 or hold_range[0]

    hold_range = [False]
    r.drives[0].hold_write = core_hold
    th, _, _ = submit_async(r, 5, 1, CLS_WT, 100)
    wait_until(lambda: r.drives[0].held, "H core write not held")

    tw, d1, rw = submit_async(r, 0, 4, CLS_WB, 101)
    time.sleep(0.3)  # W is queued on the line lock
    twi, d2, rwi = submit_async(r, 0, 2, CLS_WI, 102)
    time.sleep(0.3)  # WI looked the line up and is queued behind W
    assert tw.is_alive() and twi.is_alive()

    hold_range[0] = True  # WI's core write (or the clean before it) is held
    r.drives[0].release_writes(1)  # H completes, W gets the lock
    tw.join(10)
    assert not tw.is_alive() and rw["err"] == 0
    th.join(10)
    r.bflush()  # D1 is flushed by the user
    hold_range[0] = False
    r.drives[0].hold_write = None
    r.drives[0].release_writes()
    twi.join(10)
    assert not twi.is_alive() and rwi["err"] == 0

    r.crash()
    # blocks 2-3 are outside WI's range: dirty D1 must survive
    for i in (2, 3):
        check_after_crash(r, i, d1[i], None)
    for i in range(2):
        check_after_crash(r, i, d1[i], d2[i], older=d0[i])
    check_after_crash(r, 15, y1, None)
    r.finish()
