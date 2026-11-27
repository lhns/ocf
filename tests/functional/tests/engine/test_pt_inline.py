# SPDX-License-Identifier: BSD-3-Clause

from ctypes import c_int

import pytest

from pyocf.types.cache import Cache, CacheMode
from pyocf.types.core import Core
from pyocf.types.data import Data
from pyocf.types.io import IoDir
from pyocf.types.queue import Queue
from pyocf.types.shared import OcfCompletion
from pyocf.types.volume import RamVolume
from pyocf.types.volume_core import CoreVolume
from pyocf.utils import Size


class HeldQueue(Queue):
    """I/O queue whose thread runs only after release()"""

    held = True

    def kick(self):
        if not self.held:
            super().kick()

    def release(self):
        self.held = False
        super().kick()


def submit_held(cache_mode, pt_inline, rw):
    """
    Submits one 4 KiB I/O on a queue that does not run. Returns how many I/Os
    the core device saw before the queue ran, and the I/O's completion status.
    """
    cache = Cache.start_on_device(
        RamVolume(Size.from_MiB(50)), cache_mode=cache_mode, pt_inline=pt_inline
    )
    core_device = RamVolume(Size.from_MiB(1))
    core = Core.using_device(core_device)
    cache.add_core(core)
    vol = CoreVolume(core)
    vol.open()
    queue = HeldQueue(cache, "held")
    core_device.reset_stats()

    data = Data(4096)
    io = vol.new_io(queue, 0, data.size, rw, 0, 0)
    io.set_data(data)
    completion = OcfCompletion([("err", c_int)])
    io.callback = completion.callback
    try:
        io.submit()
        before_run = core_device.get_stats()[rw]
    finally:
        queue.release()
        completion.wait()
    assert core_device.get_stats()[rw] == 1

    vol.close()
    queue.put()
    cache.stop()
    return before_run, completion.results["err"]


@pytest.mark.parametrize("rw", [IoDir.READ, IoDir.WRITE])
@pytest.mark.parametrize("pt_inline", [True, False])
def test_pt_inline(pyocf_ctx, pt_inline, rw):
    """With pt_inline the core I/O of a pass-through request is issued before its queue runs"""
    before_run, err = submit_held(CacheMode.PT, pt_inline, rw)

    assert before_run == (1 if pt_inline else 0)
    assert err == 0


@pytest.mark.parametrize("cache_mode", [CacheMode.WT, CacheMode.WA, CacheMode.WI])
def test_pt_inline_only_pt(pyocf_ctx, cache_mode):
    """pt_inline leaves read misses in other modes to the queue"""
    before_run, err = submit_held(cache_mode, True, IoDir.READ)

    assert before_run == 0
    assert err == 0
