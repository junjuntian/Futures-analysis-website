#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_ROOT=${FUTURES_BACKFILL_STATE_ROOT:-/var/lib/futures-platform/backfill}
LOG_FILE=${FUTURES_BACKFILL_LOG_FILE:-/var/log/futures-backfill.log}
STABLE_STATE=${FUTURES_STABLE_STATE:-/var/lib/futures-platform/deployments/stable.env}
COLLECTOR_LOCK=${FUTURES_COLLECTOR_LOCK:-/run/lock/futures-collector.lock}
DRIVER_LOCK=${FUTURES_BACKFILL_DRIVER_LOCK:-/run/lock/futures-backfill-driver.lock}
DISK_PATH=${FUTURES_BACKFILL_DISK_PATH:-/var/lib/docker}
DB_SERVICE=${FUTURES_BACKFILL_DB_SERVICE:-postgres}
DB_USER=${FUTURES_BACKFILL_DB_USER:-futures_app}
DB_NAME=${FUTURES_BACKFILL_DB_NAME:-futures_platform}
SOURCE_TIMEOUT_SECONDS=${FUTURES_BACKFILL_SOURCE_TIMEOUT_SECONDS:-1800}
BLOCK_START_HHMM=${FUTURES_BACKFILL_BLOCK_START_HHMM:-1630}
BLOCK_END_HHMM=${FUTURES_BACKFILL_BLOCK_END_HHMM:-2230}

PROCESSED_FILE="$STATE_ROOT/processed_dates.tsv"
FAILURES_FILE="$STATE_ROOT/failed_dates.tsv"
SOURCE_STATE_FILE="$STATE_ROOT/source_state.tsv"
DAILY_STATE_FILE="$STATE_ROOT/daily_state.tsv"
SUMMARY_FILE="$STATE_ROOT/state.env"
EVENT_FILE="$STATE_ROOT/events.log"
RUN_LOG_ROOT="$STATE_ROOT/runs"

FROM_DATE=
TO_DATE=
MODE=backfill
DAILY_LIMIT=80
RUN_LIMIT=0
SLEEP_SECONDS=60
CONTINUOUS=0

EXCHANGES=(DCE SHFE CZCE GFEX CFFEX)
declare -A FAILURE_STREAK=()
declare -A SOURCE_PAUSED=()

usage() {
  cat <<'EOF'
Usage:
  run-backfill.sh --from YYYY-MM-DD --to YYYY-MM-DD [options]
  run-backfill.sh --retry-failures [options]

Options:
  --continuous          Wait through protected hours and daily limits.
  --daily-limit N       Trading dates attempted per Shanghai day (1..80).
  --run-limit N         Stop this invocation after N attempted dates (0 = unlimited).
  --sleep-seconds N     Delay after each attempted date (minimum 60).
  --retry-failures      Retry only unresolved date/exchange pairs.
  --status              Print persisted state without running the collector.
EOF
}

is_date() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] &&
    test "$(date -d "$1" +%F 2>/dev/null)" = "$1"
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

event() {
  local level=$1 code=$2
  shift 2
  local timestamp line
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  line="timestamp=$timestamp level=$level event=$code $*"
  printf '%s\n' "$line" | tee -a "$EVENT_FILE" "$LOG_FILE"
}

atomic_replace_without_pair() {
  local file=$1 target_date=$2 target_exchange=$3 replacement=${4:-}
  local temporary="$file.tmp.$$"
  awk -F '\t' -v date="$target_date" -v exchange="$target_exchange" \
    '!(NF >= 2 && $1 == date && $2 == exchange)' "$file" >"$temporary"
  if test -n "$replacement"; then
    printf '%s\n' "$replacement" >>"$temporary"
  fi
  sort -t $'\t' -k1,1r -k2,2 "$temporary" -o "$temporary"
  mv -f "$temporary" "$file"
}

record_failure() {
  local target_date=$1 exchange=$2 exit_code=$3 reason=$4
  local timestamp replacement
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  replacement=$(printf '%s\t%s\t%s\t%s\t%s' \
    "$target_date" "$exchange" "$timestamp" "$exit_code" "$reason")
  atomic_replace_without_pair "$FAILURES_FILE" "$target_date" "$exchange" "$replacement"
}

resolve_failure() {
  atomic_replace_without_pair "$FAILURES_FILE" "$1" "$2"
}

write_source_state() {
  local today=$1 temporary="$SOURCE_STATE_FILE.tmp.$$" exchange
  : >"$temporary"
  for exchange in "${EXCHANGES[@]}"; do
    printf '%s\t%s\t%s\t%s\n' "$today" "$exchange" \
      "${FAILURE_STREAK[$exchange]:-0}" "${SOURCE_PAUSED[$exchange]:-0}" >>"$temporary"
  done
  mv -f "$temporary" "$SOURCE_STATE_FILE"
}

load_source_state() {
  local today=$1 state_day exchange streak paused
  for exchange in "${EXCHANGES[@]}"; do
    FAILURE_STREAK[$exchange]=0
    SOURCE_PAUSED[$exchange]=0
  done
  while IFS=$'\t' read -r state_day exchange streak paused; do
    test "$state_day" = "$today" || continue
    [[ " ${EXCHANGES[*]} " == *" $exchange "* ]] || continue
    FAILURE_STREAK[$exchange]=$streak
    SOURCE_PAUSED[$exchange]=$paused
  done <"$SOURCE_STATE_FILE"
  write_source_state "$today"
}

load_daily_count() {
  local today=$1 state_day= state_count=0
  if read -r state_day state_count <"$DAILY_STATE_FILE"; then
    if test "$state_day" = "$today" && is_uint "$state_count"; then
      printf '%s' "$state_count"
      return
    fi
  fi
  printf '%s\t0\n' "$today" >"$DAILY_STATE_FILE"
  printf '0'
}

save_daily_count() { printf '%s\t%s\n' "$1" "$2" >"$DAILY_STATE_FILE"; }

next_date() { date -d "$1 - 1 day" +%F; }

disk_percent() {
  df -P "$DISK_PATH" | awk 'NR == 2 {value=$5; sub(/%$/, "", value); print value}'
}

ensure_disk_capacity() {
  local used
  used=$(disk_percent)
  if ! is_uint "$used"; then
    event ERROR disk_check_failed "path=$DISK_PATH"
    return 1
  fi
  if test "$used" -ge 80; then
    event ERROR disk_watermark_stop "path=$DISK_PATH used_percent=$used threshold_percent=80"
    return 1
  fi
}

shanghai_day() { TZ=Asia/Shanghai date +%F; }
shanghai_hhmm() { TZ=Asia/Shanghai date +%H%M; }

inside_protected_window() {
  local hhmm
  hhmm=$(shanghai_hhmm)
  test "$hhmm" -ge "$BLOCK_START_HHMM" && test "$hhmm" -lt "$BLOCK_END_HHMM"
}

wait_for_allowed_window() {
  while inside_protected_window; do
    if test "$CONTINUOUS" -ne 1; then
      event WARN protected_window_stop \
        "window=${BLOCK_START_HHMM}-${BLOCK_END_HHMM} timezone=Asia/Shanghai"
      return 1
    fi
    event INFO protected_window_wait \
      "window=${BLOCK_START_HHMM}-${BLOCK_END_HHMM} timezone=Asia/Shanghai sleep_seconds=300"
    sleep 300
  done
}

wait_for_next_budget_day() {
  local current_day=$1
  while test "$(shanghai_day)" = "$current_day"; do
    if test "$CONTINUOUS" -ne 1; then
      event INFO daily_limit_reached "date=$current_day limit=$DAILY_LIMIT"
      return 1
    fi
    event INFO daily_limit_wait "date=$current_day limit=$DAILY_LIMIT sleep_seconds=300"
    sleep 300
  done
}

psql_scalar() {
  "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -X -U "$DB_USER" -d "$DB_NAME" -Atq -v ON_ERROR_STOP=1 -c "$1"
}

calendar_status() {
  local target_date=$1
  psql_scalar "
    select case
      when exists (
        select 1 from trading_calendar_days
         where workspace_id = '$WORKSPACE_ID'::uuid
           and trade_date = date '$target_date' and is_trading_day
      ) then 'trading'
      when exists (
        select 1 from trading_calendar_days
         where workspace_id = '$WORKSPACE_ID'::uuid
           and trade_date = date '$target_date'
      ) then 'closed'
      else 'unknown'
    end"
}

date_already_complete() {
  local target_date=$1
  test "$(psql_scalar "
    with expected(exchange_code, dataset_type) as (
      values
        ('DCE','futures_catalog_v1'), ('DCE','trading_calendar_v1'),
        ('DCE','daily_market_prices_v1'), ('DCE','seat_positions_v1'),
        ('SHFE','futures_catalog_v1'), ('SHFE','trading_calendar_v1'),
        ('SHFE','daily_market_prices_v1'), ('SHFE','seat_positions_v1'),
        ('CZCE','futures_catalog_v1'), ('CZCE','trading_calendar_v1'),
        ('CZCE','daily_market_prices_v1'), ('CZCE','seat_positions_v1'),
        ('GFEX','futures_catalog_v1'), ('GFEX','trading_calendar_v1'),
        ('GFEX','daily_market_prices_v1'), ('GFEX','seat_positions_v1'),
        ('CFFEX','futures_catalog_v1'), ('CFFEX','trading_calendar_v1'),
        ('CFFEX','daily_market_prices_v1'), ('CFFEX','seat_positions_v1')
    )
    select case when count(*) = 20 then 1 else 0 end
      from expected
     where exists (
       select 1
         from import_batches batch
         join data_sources source
           on source.workspace_id = batch.workspace_id and source.id = batch.data_source_id
        where batch.workspace_id = '$WORKSPACE_ID'::uuid
          and batch.collection_date = date '$target_date'
          and batch.dataset_type = expected.dataset_type
          and batch.status = 'succeeded'
          and (
            (expected.exchange_code = 'DCE' and source.code in (
              'akshare_dce_official', 'akshare_sina_dce_fallback'
            ))
            or source.code = 'akshare_' || lower(expected.exchange_code) || '_official'
          )
     )")" = 1
}

date_was_processed() {
  awk -F '\t' -v target="$1" '$1 == target {found=1} END {exit !found}' "$PROCESSED_FILE"
}

record_processed_date() {
  local target_date=$1 result=$2 timestamp temporary="$PROCESSED_FILE.tmp.$$"
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  awk -F '\t' -v target="$target_date" '$1 != target' "$PROCESSED_FILE" >"$temporary"
  printf '%s\t%s\t%s\n' "$target_date" "$timestamp" "$result" >>"$temporary"
  sort -t $'\t' -k1,1r "$temporary" -o "$temporary"
  mv -f "$temporary" "$PROCESSED_FILE"
}

write_summary() {
  local oldest newest processed_count failed_count disk_used temporary="$SUMMARY_FILE.tmp.$$"
  oldest=$(cut -f1 "$PROCESSED_FILE" | sort | head -n 1)
  newest=$(cut -f1 "$PROCESSED_FILE" | sort -r | head -n 1)
  processed_count=$(wc -l <"$PROCESSED_FILE")
  failed_count=$(wc -l <"$FAILURES_FILE")
  disk_used=$(disk_percent)
  {
    printf 'range_from=%q\n' "${FROM_DATE:-retry-only}"
    printf 'range_to=%q\n' "${TO_DATE:-retry-only}"
    printf 'waterline_date=%q\n' "${oldest:-none}"
    printf 'newest_processed_date=%q\n' "${newest:-none}"
    printf 'processed_date_count=%q\n' "$processed_count"
    printf 'failed_pair_count=%q\n' "$failed_count"
    printf 'disk_used_percent=%q\n' "$disk_used"
    printf 'updated_at=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$temporary"
  mv -f "$temporary" "$SUMMARY_FILE"
}

run_exchange() {
  local target_date=$1 exchange=$2 run_stamp source_log status
  run_stamp=$(date -u +%Y%m%dT%H%M%SZ)
  source_log="$RUN_LOG_ROOT/${target_date}-${exchange}-${run_stamp}.log"
  event INFO source_start "date=$target_date exchange=$exchange"
  set +e
  (
    flock -w 900 9 || exit 75
    timeout --foreground "$SOURCE_TIMEOUT_SECONDS" \
      "${COMPOSE[@]}" run --rm --no-deps collector \
      --date "$target_date" --exchange "$exchange"
  ) 9>"$COLLECTOR_LOCK" 2>&1 | tee -a "$source_log" "$LOG_FILE"
  status=${PIPESTATUS[0]}
  set -e
  if test "$status" -eq 0; then
    FAILURE_STREAK[$exchange]=0
    resolve_failure "$target_date" "$exchange"
    event INFO source_succeeded "date=$target_date exchange=$exchange"
    return 0
  fi
  FAILURE_STREAK[$exchange]=$((FAILURE_STREAK[$exchange] + 1))
  record_failure "$target_date" "$exchange" "$status" collector_exit_nonzero
  event WARN source_failed "date=$target_date exchange=$exchange exit_code=$status streak=${FAILURE_STREAK[$exchange]}"
  if test "${FAILURE_STREAK[$exchange]}" -ge 5; then
    SOURCE_PAUSED[$exchange]=1
    event ERROR source_paused "exchange=$exchange streak=${FAILURE_STREAK[$exchange]} reason=consecutive_date_failures"
  fi
  return 1
}

attempt_date() {
  local target_date=$1 retry_only=$2 today daily_count exchange attempted=0 failures=0
  today=$(shanghai_day)
  load_source_state "$today"
  daily_count=$(load_daily_count "$today")
  if test "$daily_count" -ge "$DAILY_LIMIT"; then
    wait_for_next_budget_day "$today" || return 2
    today=$(shanghai_day)
    load_source_state "$today"
    daily_count=$(load_daily_count "$today")
  fi
  wait_for_allowed_window || return 2
  ensure_disk_capacity || return 3

  daily_count=$((daily_count + 1))
  save_daily_count "$today" "$daily_count"
  event INFO date_start "date=$target_date daily_count=$daily_count daily_limit=$DAILY_LIMIT mode=$MODE"

  for exchange in "${EXCHANGES[@]}"; do
    if test "$retry_only" -eq 1 &&
      ! awk -F '\t' -v date="$target_date" -v source="$exchange" \
        '$1 == date && $2 == source {found=1} END {exit !found}' "$FAILURES_FILE"; then
      continue
    fi
    attempted=1
    if test "${SOURCE_PAUSED[$exchange]:-0}" -eq 1; then
      record_failure "$target_date" "$exchange" 75 source_paused_for_day
      event WARN source_skipped_paused "date=$target_date exchange=$exchange"
      failures=$((failures + 1))
      continue
    fi
    if ! wait_for_allowed_window; then
      record_failure "$target_date" "$exchange" 75 protected_window_started
      failures=$((failures + 1))
      continue
    fi
    if ! run_exchange "$target_date" "$exchange"; then
      failures=$((failures + 1))
    fi
    write_source_state "$today"
  done

  if test "$attempted" -eq 0; then
    event INFO date_no_pending_sources "date=$target_date"
    return 0
  fi
  if test "$retry_only" -eq 0; then
    if test "$failures" -eq 0; then
      record_processed_date "$target_date" succeeded
    else
      record_processed_date "$target_date" partial
    fi
  fi
  write_summary
  event INFO date_finished "date=$target_date failed_sources=$failures sleep_seconds=$SLEEP_SECONDS"
  sleep "$SLEEP_SECONDS"
  return 0
}

print_status() {
  test -s "$SUMMARY_FILE" && cat "$SUMMARY_FILE" || echo state=empty
  printf 'failed_pairs=%s\n' "$(wc -l <"$FAILURES_FILE")"
  printf 'disk_used_percent=%s\n' "$(disk_percent)"
  printf 'driver_running=%s\n' "$(
    if flock -n 8; then echo no; else echo yes; fi
  )"
}

STATUS_ONLY=0
while test "$#" -gt 0; do
  case "$1" in
    --from) FROM_DATE=${2:-}; shift 2 ;;
    --to) TO_DATE=${2:-}; shift 2 ;;
    --daily-limit) DAILY_LIMIT=${2:-}; shift 2 ;;
    --run-limit) RUN_LIMIT=${2:-}; shift 2 ;;
    --sleep-seconds) SLEEP_SECONDS=${2:-}; shift 2 ;;
    --continuous) CONTINUOUS=1; shift ;;
    --retry-failures) MODE=retry; shift ;;
    --status) STATUS_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; echo "BACKFILL_FAIL unknown_argument=$1" >&2; exit 64 ;;
  esac
done

is_uint "$DAILY_LIMIT" && test "$DAILY_LIMIT" -ge 1 && test "$DAILY_LIMIT" -le 80 || {
  echo "BACKFILL_FAIL daily_limit_must_be_1_to_80" >&2; exit 64;
}
is_uint "$RUN_LIMIT" || { echo "BACKFILL_FAIL run_limit_must_be_nonnegative" >&2; exit 64; }
is_uint "$SLEEP_SECONDS" && test "$SLEEP_SECONDS" -ge 60 || {
  echo "BACKFILL_FAIL sleep_seconds_must_be_at_least_60" >&2; exit 64;
}
is_uint "$SOURCE_TIMEOUT_SECONDS" && test "$SOURCE_TIMEOUT_SECONDS" -ge 60 || {
  echo "BACKFILL_FAIL source_timeout_must_be_at_least_60" >&2; exit 64;
}

install -d -m 700 "$STATE_ROOT" "$RUN_LOG_ROOT"
touch "$PROCESSED_FILE" "$FAILURES_FILE" "$SOURCE_STATE_FILE" "$DAILY_STATE_FILE" \
  "$EVENT_FILE" "$LOG_FILE"
chmod 600 "$PROCESSED_FILE" "$FAILURES_FILE" "$SOURCE_STATE_FILE" "$DAILY_STATE_FILE" \
  "$SUMMARY_FILE" "$EVENT_FILE" "$LOG_FILE" 2>/dev/null || true

exec 8>"$DRIVER_LOCK"
if test "$STATUS_ONLY" -eq 1; then
  print_status
  exit 0
fi
flock -n 8 || { echo "BACKFILL_FAIL driver_already_running" >&2; exit 75; }

if test "$MODE" = backfill; then
  test -n "$FROM_DATE" && test -n "$TO_DATE" || { usage >&2; exit 64; }
  is_date "$FROM_DATE" && is_date "$TO_DATE" || {
    echo "BACKFILL_FAIL invalid_date_range" >&2; exit 64;
  }
  [[ "$FROM_DATE" < "$TO_DATE" || "$FROM_DATE" = "$TO_DATE" ]] || {
    echo "BACKFILL_FAIL from_after_to" >&2; exit 64;
  }
fi

test -r "$STABLE_STATE"
# shellcheck disable=SC1090
. "$STABLE_STATE"
test -n "${previous_git_sha:-}" && test -n "${previous_release_dir:-}"
case "$previous_release_dir" in
  /opt/futures-platform-releases/*) ;;
  *) echo "BACKFILL_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac
test "$previous_git_sha" = "e627ab8c3b797cc77f872a9c02439c1dfca0d4eb" || {
  echo "BACKFILL_FAIL unexpected_runtime_candidate" >&2; exit 1;
}
export IMAGE_TAG="sha-${previous_git_sha}"
COMPOSE=(
  docker compose
  -f "$previous_release_dir/docker-compose.yml"
  -f "$previous_release_dir/docker-compose.production.yml"
  -f "$previous_release_dir/docker-compose.release.yml"
  --profile collector
)
"${COMPOSE[@]}" config --format json | jq -e --arg image "$previous_collector_ref" '
  .services.collector.image == $image
  and (.services.collector.mem_limit | tostring) == "536870912"
' >/dev/null || {
  echo "BACKFILL_FAIL collector_runtime_contract_mismatch" >&2; exit 1;
}

WORKSPACE_ID=$(psql_scalar "
  select workspace_id
    from data_sources
   where code in (
     'akshare_dce_official','akshare_shfe_official','akshare_czce_official',
     'akshare_gfex_official','akshare_cffex_official'
   )
   group by workspace_id
  having count(distinct code) = 5
   order by workspace_id
   limit 1")
[[ "$WORKSPACE_ID" =~ ^[0-9a-f-]{36}$ ]] || {
  echo "BACKFILL_FAIL collector_workspace_not_found" >&2; exit 1;
}

ensure_disk_capacity
event INFO driver_started \
  "mode=$MODE from=${FROM_DATE:-none} to=${TO_DATE:-none} daily_limit=$DAILY_LIMIT run_limit=$RUN_LIMIT runtime_sha=$previous_git_sha"

attempted_this_run=0
if test "$MODE" = retry; then
  while IFS= read -r target_date; do
    test -n "$target_date" || continue
    if test "$RUN_LIMIT" -gt 0 && test "$attempted_this_run" -ge "$RUN_LIMIT"; then break; fi
    attempt_date "$target_date" 1 || status=$?
    status=${status:-0}
    if test "$status" -eq 2; then break; fi
    if test "$status" -eq 3; then exit 75; fi
    attempted_this_run=$((attempted_this_run + 1))
    status=0
  done < <(cut -f1 "$FAILURES_FILE" | sort -r -u)
else
  candidate=$TO_DATE
  while [[ "$candidate" > "$FROM_DATE" || "$candidate" = "$FROM_DATE" ]]; do
    if test "$RUN_LIMIT" -gt 0 && test "$attempted_this_run" -ge "$RUN_LIMIT"; then break; fi
    if date_was_processed "$candidate"; then
      candidate=$(next_date "$candidate")
      continue
    fi
    day_status=$(calendar_status "$candidate")
    weekday=$(date -d "$candidate" +%u)
    if test "$day_status" = closed || { test "$day_status" = unknown && test "$weekday" -ge 6; }; then
      record_processed_date "$candidate" "calendar_${day_status}"
      write_summary
      candidate=$(next_date "$candidate")
      continue
    fi
    if date_already_complete "$candidate"; then
      record_processed_date "$candidate" already_succeeded
      write_summary
      candidate=$(next_date "$candidate")
      continue
    fi
    status=0
    attempt_date "$candidate" 0 || status=$?
    if test "$status" -eq 2; then break; fi
    if test "$status" -eq 3; then exit 75; fi
    attempted_this_run=$((attempted_this_run + 1))
    candidate=$(next_date "$candidate")
  done
fi

write_summary
event INFO driver_finished "mode=$MODE attempted_dates=$attempted_this_run"
