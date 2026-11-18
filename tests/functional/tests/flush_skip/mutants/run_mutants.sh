#!/bin/bash
#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#
# Flush-skip mutation check: for each mutant, patch a scratch copy of the ocf
# tree with mutate.py, rebuild pyocf and run tests/flush_skip. Every mutant
# must make at least one test fail. See README.md.
#
# usage: run_mutants.sh [mutant ...]     (default: all nine)
# env:   WORKDIR  scratch dir (default: a new mktemp dir, kept for the logs)
#        JOBS     make -j (default 4)      XDIST  pytest -n (default 3)

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../../../../.." && pwd)    # ocf repo root
JOBS=${JOBS:-4}
XDIST=${XDIST:-3}
WORKDIR=${WORKDIR:-$(mktemp -d /tmp/flush-skip-mutants.XXXXXX)}
mutants=("$@")
[ ${#mutants[@]} -gt 0 ] || mutants=(always_skip flag_clear_on_completion flag_clear_on_issue seq_cutoff_uncounted discard_uncounted flushed_on_failure done_at_submit cleaner_counted flushed_no_max)

mkdir -p "$WORKDIR/mut"
summary=$WORKDIR/mut.summary
: > "$summary"
echo "workdir: $WORKDIR"
bad=0
for m in "${mutants[@]}"; do
  dir=$WORKDIR/mut/$m
  rm -rf "$dir"; mkdir -p "$dir"
  (cd "$root" && tar --exclude=.git -cf - .) | tar -xf - -C "$dir"
  cd "$dir"; rm -rf tests/functional/pyocf/ocf tests/functional/pyocf/libocf.so; find . -name '*.o' -delete
  python3 "$here/mutate.py" "$m" "$dir" > "$dir.mutlog" 2>&1 || { echo "$m MUTATE-FAILED $(tail -1 "$dir.mutlog")" >> "$summary"; bad=1; continue; }
  cd tests/functional; make LDFLAGS="-pthread -lz" -j"$JOBS" > "$dir.build" 2>&1 || { echo "$m BUILD-FAILED" >> "$summary"; bad=1; continue; }
  timeout 1500 python3 -m pytest tests/flush_skip -q -p no:cacheprovider --timeout=300 --tb=line --show-capture=no -n "$XDIST" -rf > "$dir.log" 2>&1
  res=$(grep -E ' passed| failed' "$dir.log" | tail -1)
  echo "$m: $res" >> "$summary"
  case $res in *failed*) ;; *) echo "$m: SURVIVED" >> "$summary"; bad=1 ;; esac
  rm -rf "$dir"
done
cat "$summary"
[ $bad -eq 0 ] && echo "all ${#mutants[@]} mutants killed" || echo "FAILED: see $summary"
exit $bad
