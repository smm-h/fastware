#!/usr/bin/env bash
# Run a pytest selection N times and report how many runs passed.
#
# Flakiness in this suite comes from real background servers (granian) reacting
# to system load, so a single green run proves nothing. Use this to get a pass
# rate instead.
#
# Usage: scripts/repeat_tests.sh <runs> [pytest args...]
#   scripts/repeat_tests.sh 10 tests/test_server.py tests/test_serve_background.py
#   scripts/repeat_tests.sh 3           # whole suite
set -u

if [ $# -lt 1 ]; then
    echo "usage: $0 <runs> [pytest args...]" >&2
    exit 2
fi

runs="$1"
shift

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

log_dir="${REPEAT_TESTS_LOG_DIR:-$repo_root/.pytest_cache/repeat-runs}"
mkdir -p "$log_dir"

pass=0
fail=0
failed_runs=()

for i in $(seq 1 "$runs"); do
    log="$log_dir/run-$i.log"
    if uv run pytest "$@" -q >"$log" 2>&1; then
        pass=$((pass + 1))
        printf 'run %s/%s: PASS\n' "$i" "$runs"
    else
        fail=$((fail + 1))
        failed_runs+=("$i")
        printf 'run %s/%s: FAIL (%s)\n' "$i" "$runs" "$log"
        tail -n 25 "$log"
    fi
done

printf '\n%s/%s passed, %s failed\n' "$pass" "$runs" "$fail"
if [ "$fail" -gt 0 ]; then
    printf 'failing runs: %s\n' "${failed_runs[*]}"
    exit 1
fi
