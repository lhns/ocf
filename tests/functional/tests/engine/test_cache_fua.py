#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
A write with FUA must be durable on the cache device too, not only on the core:
the cache device may hold the only copy (dirty data) or the older copy of it.

Both devices have a volatile write cache and treat one I/O flag bit as FUA.
Crash model as in test_wt_over_dirty.py, but the cache device image is what
survived the power cut. The cache is then loaded (dirty shutdown recovery) and
read back; a cache flush must not write older data to the core either.
"""

import pytest

from pyocf.types.cache import CacheMode

from .test_wt_over_dirty import Rig, BLK, ZERO
from .wt_crash_devices import VolatileCache, pattern

FUA = 0x1000


class FuaRig(Rig):
    cache_dev_type = VolatileCache
    fua_flag = FUA

    def __init__(self, ctx, mode):
        super().__init__(ctx, mode)
        self.cache_ram.make_durable()  # format done


def check_fua_durable(r, blk, want):
    assert r.read(blk) == want, "read after power cut returns older data"
    r.cache.flush()
    r.drives[0].release_all()
    core = bytes(r.drives[0].durable)[blk * BLK:(blk + 1) * BLK]
    assert core == want, "cache flush wrote older data to the core"


@pytest.mark.parametrize("mode", [CacheMode.WT, CacheMode.WB, CacheMode.WA, CacheMode.WO])
def test_fua_write_over_dirty(pyocf_ctx, mode):
    """Dirty data D1 (made durable by a flush), then a FUA write D2 in the mode under test"""
    r = FuaRig(pyocf_ctx, CacheMode.WB)
    r.write(0)
    r.bflush()
    r.cache.change_cache_mode(mode)
    assert r.dirty_lines() == 1
    d2 = r.write(0, flags=FUA)
    assert r.last_err == 0
    r.crash()
    check_fua_durable(r, 0, d2)
    r.finish()


@pytest.mark.parametrize("mode", [CacheMode.WB, CacheMode.WO])
def test_fua_write_miss(pyocf_ctx, mode):
    """FUA write to a line not in the cache: dirty metadata and data must both be durable"""
    r = FuaRig(pyocf_ctx, mode)
    d = r.write(5, flags=FUA)
    assert r.last_err == 0
    r.crash()
    check_fua_durable(r, 5, d)
    r.finish()


def test_fua_write_over_clean_hit_is_not_lost(pyocf_ctx):
    """Control: clean lines are dropped by recovery, the core copy is durable"""
    r = FuaRig(pyocf_ctx, CacheMode.WT)
    r.write(0)
    r.bflush()
    d2 = r.write(0, flags=FUA)
    r.crash()
    check_fua_durable(r, 0, d2)
    r.finish()
