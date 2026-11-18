# Flush-skip mutation check

The flush-skip tests (`tests/flush_skip`) are only worth something if they fail
when the feature is wrong. `mutate.py` injects nine known bugs, one per run,
into a scratch copy of the OCF sources; `run_mutants.sh` rebuilds pyocf for
each and runs `tests/flush_skip`. A mutant is **killed** if at least one test
fails. A surviving mutant means a hole in the tests (or a stale anchor, see
below).

This proves the tests can fail for these specific bugs. It does not prove the
feature is correct or that the tests cover every other bug.

## Running

Prerequisites: the same as the pyocf functional tests (gcc, make, zlib headers,
Python 3 with `pytest`, `pytest-xdist`, `pytest-timeout`, `PyYAML`, `six`;
validated with Python 3.9). No kernel module is needed.

From the ocf repo root or from `tests/functional` (the script locates the repo
from its own path):

    tests/functional/tests/flush_skip/mutants/run_mutants.sh [mutant ...]

Without arguments all nine mutants run (about 3 minutes each). Environment:
`WORKDIR` (scratch dir, default a fresh `/tmp/flush-skip-mutants.*`, kept for
the per-mutant `.log`/`.build`/`.mutlog` files), `JOBS` (make -j, default 4),
`XDIST` (pytest -n, default 3). The checkout itself is never modified; each
mutant runs in a copy without `.git`. Output ends with one line per mutant
(`name: N failed, M passed`) and the exit code is non-zero if any mutant
survived or failed to patch/build.

`mutate.py NAME ROOT` can also be used alone to patch a tree in place.

## Mutants

| Mutant | What it breaks |
|---|---|
| `always_skip` | Skip every flush while the feature is enabled, ignoring whether writes reached the core. |
| `flag_clear_on_completion` | Tracks "dirty" as a flag cleared on write completion instead of a done/flushed counter pair. |
| `flag_clear_on_issue` | Same flag, but cleared when the flush is issued rather than when it succeeds. |
| `seq_cutoff_uncounted` | Writes that took the sequential cut-off path are not counted as completed core writes. |
| `discard_uncounted` | Discards are not counted as core writes. |
| `flushed_on_failure` | A failed core flush still advances the flushed generation. |
| `done_at_submit` | The write is counted as done when submitted, not when the core completes it. |
| `cleaner_counted` | Cleaner writes are counted like user writes. |
| `flushed_no_max` | The flushed generation is overwritten instead of only moved forward (a stale flush can regress it). |

## Expected result (as of ocf 82049b3c)

Failed tests per mutant: `always_skip` 55, `flag_clear_on_completion` 2,
`flag_clear_on_issue` 6, `seq_cutoff_uncounted` 1, `discard_uncounted` 1,
`flushed_on_failure` 4, `done_at_submit` 2, `cleaner_counted` 1,
`flushed_no_max` 1. Only "at least one failure" is required; the counts change
when tests are added.

## Keeping the anchors current

`mutate.py` replaces exact source text and asserts it occurs once, so it fails
with `MUTATE-FAILED` when the patched code is reworded or moves. Update the
anchor then, and re-check that the mutant still compiles and is killed. The
anchors live in `src/ocf_core.c` and `src/ocf_request.c`. The
`ocf_forward_end()` anchors are the fragile ones: branch
`fix/flush-skip-composite` rewrites that function (flush counted on the last
forward end, `if (!req->core_error)` one indentation level deeper), so
`flushed_on_failure` needs its anchor changed there, and
`flushed_no_max`/`seq_cutoff_uncounted` should be re-verified against it.
