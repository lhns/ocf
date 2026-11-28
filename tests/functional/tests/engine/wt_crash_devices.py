#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
"""
Devices for crash tests (see test_wt_over_dirty.py).

VolatileVolume models a drive with a volatile write cache: a write completed
to OCF is only durable after a flush completed. GuardedRam is a cache device
whose content can be copied atomically between two I/Os. Both can fail
writes selected by a predicate (fail_write).
"""

import threading
from ctypes import cast, c_void_p, memmove, memset, string_at

from pyocf.ocf import OcfLib
from pyocf.types.data import Data
from pyocf.types.io import Io, IoDir
from pyocf.types.volume import Volume, RamVolume
from pyocf.types.shared import OcfErrorCode
from pyocf.utils import Size

BLK = 4096

lib = OcfLib.getInstance()


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
    A write carrying the fua_flag bit (0: none) is durable when it completes.
    """

    fua_flag = 0

    def __init__(self, size_bytes, uuid=None):
        super().__init__(uuid)
        self.size = size_bytes
        self.durable = bytearray(size_bytes)
        self.pending = []  # (id, ("w", addr, bytes) | ("z", addr, nbytes))
        self.next_id = 1
        self.trace = []  # drive level events, for failure reports
        self.lock = threading.RLock()

        self.hold_write = None  # predicate (kind, addr, nbytes) -> bool
        self.fail_write = None  # predicate (kind, addr, nbytes) -> bool
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

        fua = bool(self.fua_flag & lib.ocf_forward_get_flags(token))
        self._write_or_hold(token, ("w", addr, string_at(data, nbytes)), fua)

    def do_forward_discard(self, token, addr, nbytes):
        self._write_or_hold(token, ("z", addr, nbytes))

    def _write_or_hold(self, token, op, fua=False):
        kind, addr, arg = op
        nbytes = len(arg) if kind == "w" else arg
        if self.fail_write and self.fail_write(kind, addr, nbytes):
            Io.forward_end(token, -OcfErrorCode.OCF_ERR_IO)
            return
        with self.lock:
            if self.hold_write and self.hold_write(kind, addr, nbytes):
                self.held.append((token, op, fua))
                return
        self._complete_write(token, op, fua)

    def _complete_write(self, token, op, fua=False):
        with self.lock:
            self.pending.append((self.next_id, op))
            if fua:  # also stays pending, so a later flush keeps the write order
                self.apply(self.durable, op)
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
                token, op, fua = self.held.pop(0)
                if count is not None:
                    count -= 1
            self._complete_write(token, op, fua)

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

    def make_durable(self):
        """As if a flush completed now"""
        with self.lock:
            for _, op in self.pending:
                self.apply(self.durable, op)
            self.pending = []

    def release_all(self):
        self.hold_write = None
        self.hold_flush = False
        self.release_writes()
        self.release_flushes()


class VolatileCache(VolatileVolume):
    """Cache device with a volatile write cache; snapshot() is its image after a power cut"""

    def snapshot(self):
        return self.crash_image()


class GuardedRam(RamVolume):
    """
    Cache device whose content can be copied between two I/Os: every I/O is
    atomic with respect to a crash image, as on a real device.
    """

    def __init__(self, size, uuid=None):
        super().__init__(size, uuid)
        self.io_lock = threading.Lock()
        self.io_count = 0
        self.fail_write = None  # predicate (addr, nbytes) -> bool

    def do_forward_io(self, token, rw, addr, nbytes, offset):
        try:
            with self.io_lock:
                self.io_count += 1
                if rw == IoDir.WRITE and self.fail_write and self.fail_write(addr, nbytes):
                    raise IOError("injected write error")
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

