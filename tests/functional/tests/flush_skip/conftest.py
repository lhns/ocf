#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#

import pytest

from . import harness


@pytest.fixture(autouse=True)
def flush_skip_cleanup(pyocf_ctx):
    """
    A failed assertion must not leave held requests behind: the context teardown
    would wait for them forever. Runs before the pyocf_ctx teardown.
    """
    yield
    for h in harness.ACTIVE:
        h.emergency_stop()
    harness.ACTIVE.clear()
