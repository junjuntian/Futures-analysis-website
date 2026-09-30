#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat >"$work/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"$TEST_WORK/sql-call"
if [[ "${MOCK_DB_FAIL:-0}" == 1 ]]; then exit 1; fi
if [[ "${MOCK_READY:-0}" == 1 ]]; then printf '%s\n' "${MOCK_FINGERPRINT:-ws:LH00}"; fi
EOF
cat >"$work/bin/systemd-run" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TEST_WORK/dispatches"
EOF
cat >"$work/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$work/bin/runner" <<'EOF'
#!/usr/bin/env bash
printf 'run\n' >>"$TEST_WORK/runs"
[[ "${MOCK_RUN_FAIL:-0}" != 1 ]]
EOF
chmod +x "$work/bin/"*

export TEST_WORK="$work"
export PATH="$work/bin:$PATH"
export SMART_MONEY_READY_STATE_DIR="$work/state"
export SMART_MONEY_READY_LOG="$work/engine.log"
export SMART_MONEY_READY_LOCK="$work/ready.lock"
export SMART_MONEY_READY_RUNNER="$work/bin/runner"
export SMART_MONEY_READY_SCRIPT="$root/engine/run-smart-money-on-ready.sh"
today=$(TZ=Asia/Shanghai date +%F)

MOCK_READY=0 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"
test ! -e "$work/dispatches"
if MOCK_DB_FAIL=1 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"; then
  echo "database failure was treated as incomplete data" >&2
  exit 1
fi

MOCK_READY=1 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"
test "$(wc -l <"$work/dispatches")" -eq 1
grep -q -- "--run $today" "$work/dispatches"
grep -q "count(distinct exchange)" "$work/sql-call"
grep -q "workspace_id" "$work/sql-call"
grep -q "reboard_inferred" "$work/sql-call"
grep -q "settlement_price > 0" "$work/sql-call"
grep -q "open_interest is not null" "$work/sql-call"
grep -q "('LH')" "$work/sql-call"

if MOCK_READY=1 MOCK_RUN_FAIL=1 bash "$SMART_MONEY_READY_SCRIPT" --run "$today"; then
  echo "failed engine was marked successful" >&2
  exit 1
fi
test ! -e "$SMART_MONEY_READY_STATE_DIR/$today.done"
MOCK_READY=1 bash "$SMART_MONEY_READY_SCRIPT" --run "$today"
MOCK_READY=1 bash "$SMART_MONEY_READY_SCRIPT" --run "$today"
test "$(wc -l <"$work/runs")" -eq 2
test -e "$SMART_MONEY_READY_STATE_DIR/$today.done"
test "$(<"$SMART_MONEY_READY_STATE_DIR/$today.done")" == 'ws:LH00'
MOCK_READY=1 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"
test "$(wc -l <"$work/dispatches")" -eq 1

# 同一天稍后补齐生猪行情/席位，应重新触发；相同输入不重复计算。
MOCK_READY=1 MOCK_FINGERPRINT=ws:LH11 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"
test "$(wc -l <"$work/dispatches")" -eq 2
MOCK_READY=1 MOCK_FINGERPRINT=ws:LH11 bash "$SMART_MONEY_READY_SCRIPT" --run "$today"
test "$(<"$SMART_MONEY_READY_STATE_DIR/$today.done")" == 'ws:LH11'
MOCK_READY=1 MOCK_FINGERPRINT=ws:LH11 bash "$SMART_MONEY_READY_SCRIPT" --dispatch "$today"
test "$(wc -l <"$work/dispatches")" -eq 2
test "$(wc -l <"$work/runs")" -eq 3

echo "smart-money ready trigger OK"
