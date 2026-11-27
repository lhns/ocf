# SPDX-License-Identifier: BSD-3-Clause
"""
use_submit_io_fast must not change what a write does: a write hit in a
write-through mode goes to the core and leaves the line clean.
"""

import pytest

from pyocf.types.cache import Cache, CacheMode
from pyocf.types.core import Core
from pyocf.types.data import Data
from pyocf.types.io import IoDir, Sync
from pyocf.types.volume import RamVolume
from pyocf.types.volume_core import CoreVolume
from pyocf.utils import Size

BS = 4096


def io(vol, queue, rw, fill=None):
    data = Data(BS)
    if fill is not None:
        data.write(fill * BS, BS)
    req = vol.new_io(queue, 0, BS, rw, 0, 0)
    req.set_data(data)
    assert Sync(req).submit().results["err"] == 0
    return data


@pytest.mark.parametrize("cache_mode", [CacheMode.WT, CacheMode.WA])
def test_submit_fast_write_hit(pyocf_ctx, cache_mode):
    cache = Cache.start_on_device(
        RamVolume(Size.from_MiB(50)), cache_mode=cache_mode, use_submit_fast=True
    )
    core_device = RamVolume(Size.from_MiB(1))
    core = Core.using_device(core_device)
    cache.add_core(core)
    vol = CoreVolume(core)
    vol.open()
    queue = cache.get_default_queue()

    io(vol, queue, IoDir.WRITE, b"a")
    io(vol, queue, IoDir.READ)  # wa promotes on read
    io(vol, queue, IoDir.WRITE, b"b")  # write hit

    assert cache.get_stats()["usage"]["dirty"]["value"] == 0
    assert bytes(core_device.get_bytes()[:BS]) == b"b" * BS

    vol.close()
    cache.stop()
