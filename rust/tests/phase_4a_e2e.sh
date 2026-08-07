#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

report_failure() {
  local status=$? line=$1
  echo "PHASE4A_E2E_FAIL line=$line status=$status" >&2
  exit "$status"
}
trap 'report_failure "$LINENO"' ERR

RELEASE_DIR=${PHASE4A_RELEASE_DIR:?set PHASE4A_RELEASE_DIR}
COLLECTION_DATE=${PHASE4A_COLLECTION_DATE:?set PHASE4A_COLLECTION_DATE}
EVIDENCE_DIR=${PHASE4A_EVIDENCE_DIR:?set PHASE4A_EVIDENCE_DIR}
DB_SERVICE=${PHASE4A_DB_SERVICE:-postgres}
DB_USER=${PHASE4A_DB_USER:-futures_app}
DB_NAME=${PHASE4A_DB_NAME:-futures_platform}
CONTAINER_NAME=phase4a-collector-e2e
BASE=${PHASE4A_BASE_URL:-http://127.0.0.1:8088}
ORIGIN=${PHASE4A_ORIGIN:-http://localhost:8088}
COOKIE_NAME=${PHASE4A_COOKIE_NAME:-futures_session}
E2E_COLLECTOR_SESSION=
E2E_ADMIN_SESSION=

case "$RELEASE_DIR" in
  /opt/futures-platform-releases/*) ;;
  *) echo "PHASE4A_E2E_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac
[[ "$COLLECTION_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
echo "PHASE4A_E2E_STAGE preconditions"
install -d -m 700 "$EVIDENCE_DIR"
test -s /etc/futures-platform/secrets/collector-credentials
test "$(stat -c %U:%G /etc/futures-platform/secrets/collector-credentials)" = root:root
test "$(stat -c %a /etc/futures-platform/secrets/collector-credentials)" = 400
test "$(stat -c %a /etc/cron.d/futures-collector)" = 600
test "$(grep -c '^30 17 \* \* 1-5 root ' /etc/cron.d/futures-collector)" = 1
test "$(grep -c '^30 21 \* \* 1-5 root ' /etc/cron.d/futures-collector)" = 1
test -x /usr/local/sbin/run-futures-collector
echo "PHASE4A_E2E_STAGE preconditions_passed"

COMPOSE=(
  docker compose
  -f "$RELEASE_DIR/docker-compose.yml"
  -f "$RELEASE_DIR/docker-compose.production.yml"
  -f "$RELEASE_DIR/docker-compose.release.yml"
  --profile collector
)
"${COMPOSE[@]}" config --format json | jq -e '
  .services.collector.tmpfs
  | any(startswith("/work:") and contains("size=128m") and contains("mode=0700"))
' >/dev/null
"${COMPOSE[@]}" config --format json | jq -e \
  '.services.collector.environment.COLLECTOR_TEMP_ROOT == "/work" and .services.collector.environment.TMPDIR == "/work"' \
  >/dev/null

psql_value() {
  "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -X -U "$DB_USER" -d "$DB_NAME" -Atq -v ON_ERROR_STOP=1 "$@"
}

new_uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
hash_token() {
  printf '%s' "$1" | openssl dgst -sha256 -binary |
    base64 | tr '+/' '-_' | tr -d '=\n'
}
assert_status() {
  test "$1" = "$2" || {
    echo "PHASE4A_E2E_FAIL http_label=$3 expected=$2 actual=$1" >&2
    exit 1
  }
}
api_get() {
  local token=$1 path=$2 output=$3
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" "$BASE$path"
}
api_json() {
  local token=$1 csrf=$2 method=$3 path=$4 body=$5 output=$6 key=${7:-}
  local headers=(-H "Cookie: $COOKIE_NAME=$token" -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" -H 'Content-Type: application/json')
  test -z "$key" || headers+=(-H "Idempotency-Key: $key")
  curl -sS -o "$output" -w '%{http_code}' -X "$method" \
    "${headers[@]}" --data "$body" "$BASE$path"
}
manual_upload() {
  local token=$1 csrf=$2 file=$3 output=$4
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" -F "file=@$file;type=text/csv" "$BASE/api/v1/imports"
}
automatic_upload() {
  local token=$1 csrf=$2 file=$3 dataset=$4 source=$5 output=$6
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" -H 'x-ingestion-mode: automatic' \
    -H "x-dataset-type: $dataset" -H "x-data-source-code: $source" \
    -H "x-collection-date: $COLLECTION_DATE" -H "x-template-version: $dataset@1" \
    -F "file=@$file;type=text/csv" "$BASE/api/v1/imports"
}
wait_batch() {
  local token=$1 import_id=$2 expected=$3
  local output="$EVIDENCE_DIR/wait-$import_id.json"
  for _ in $(seq 1 360); do
    if test "$(api_get "$token" "/api/v1/imports/$import_id" "$output")" = 200; then
      local status
      status=$(jq -r '.data.status' "$output")
      test "$status" = "$expected" && return 0
      case "$status" in failed|dead_letter|rollback_conflict|rollback_failed)
        echo "PHASE4A_E2E_FAIL batch_terminal_status=$status" >&2; exit 1 ;;
      esac
    fi
    sleep 0.25
  done
  echo "PHASE4A_E2E_FAIL batch_timeout" >&2
  exit 1
}
confirm_automatic() {
  local import_id=$1 label=$2
  local output="$EVIDENCE_DIR/$label-confirm.json"
  assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" POST "/api/v1/imports/$import_id/automatic-confirm" '{}' "$output" "phase4a-$label-confirm-$import_id")" 202 "$label automatic confirm"
  wait_batch "$COLLECTOR_TOKEN" "$import_id" succeeded
}
create_automatic_batch() {
  local file=$1 dataset=$2 source=$3 label=$4
  local output="$EVIDENCE_DIR/$label-upload.json"
  assert_status "$(automatic_upload "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" "$file" "$dataset" "$source" "$output")" 201 "$label automatic upload"
  local import_id
  import_id=$(jq -r '.data.id' "$output")
  confirm_automatic "$import_id" "$label"
  printf '%s' "$import_id"
}
rollback_check() {
  local import_id=$1 output=$2
  api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$import_id/rollback-check" '{}' "$output"
}
rollback_batch() {
  local import_id=$1 label=$2
  local check="$EVIDENCE_DIR/$label-rollback-check.json"
  local output="$EVIDENCE_DIR/$label-rollback.json"
  assert_status "$(rollback_check "$import_id" "$check")" 200 "$label rollback check"
  jq -e '.data.can_rollback == true and .data.conflict_count == 0' "$check" >/dev/null
  local request_id fingerprint
  request_id=$(jq -r '.data.precheck_request_id' "$check")
  fingerprint=$(jq -r '.data.precheck_fingerprint' "$check")
  assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$import_id/rollback" "{\"precheck_request_id\":\"$request_id\",\"precheck_fingerprint\":\"$fingerprint\"}" "$output" "phase4a-$label-rollback-$import_id")" 202 "$label rollback"
  wait_batch "$ADMIN_TOKEN" "$import_id" rolled_back
}
cleanup_e2e_sessions() {
  local status=$?
  set +e
  if [[ "$E2E_COLLECTOR_SESSION" =~ ^[0-9a-f-]{36}$ ]] && [[ "$E2E_ADMIN_SESSION" =~ ^[0-9a-f-]{36}$ ]]; then
    psql_value -c "delete from sessions where id in ('$E2E_COLLECTOR_SESSION','$E2E_ADMIN_SESSION')" >/dev/null 2>&1
  fi
  exit "$status"
}
trap cleanup_e2e_sessions EXIT

legacy_batches_before=$(psql_value -c \
  "select count(*) from import_batches where ingestion_mode='manual'")
legacy_batches_fingerprint_before=$(psql_value -c \
  "select md5(coalesce(string_agg(to_jsonb(batch)::text, '|' order by id), '')) from import_batches batch where ingestion_mode='manual'")
automatic_batches_before=$(psql_value -c \
  "select count(*) from import_batches where ingestion_mode='automatic'")
users_before=$(psql_value -c "select count(*) from users")
users_identity_fingerprint_before=$(psql_value -c \
  "select md5(coalesce(string_agg((to_jsonb(app_user) - 'updated_at' - 'last_login_at')::text, '|' order by id), '')) from users app_user")
echo "PHASE4A_E2E_BASELINE manual_batches=$legacy_batches_before automatic_batches=$automatic_batches_before users=$users_before"
test "$legacy_batches_before" -ge 127
echo "PHASE4A_E2E_STAGE baseline_counts_passed"

# Deploys that do not touch collection or Phase 4A run a light regression
# instead of three live 5-exchange collector passes, which otherwise spend
# about an hour on the DCE 412 fallback and timeout path unrelated to the
# change under test. The full gate still runs by default; the deploy workflow
# sets PHASE4A_RUN_LIVE_COLLECTION=false when the candidate is unrelated.
if [[ "${PHASE4A_RUN_LIVE_COLLECTION:-true}" != "true" ]]; then
  echo "PHASE4A_E2E_STAGE live_collection_skipped reason=deploy_unrelated_to_collection"
  test "$(psql_value -c "select count(*) from market_prices")" -gt 0
  test "$(psql_value -c "select count(*) from seat_positions")" -gt 0
  test "$(psql_value -c "select count(*) from contracts")" -gt 0
  test "$(psql_value -c "select md5(coalesce(string_agg(to_jsonb(batch)::text, '|' order by id), '')) from import_batches batch where ingestion_mode='manual'")" = "$legacy_batches_fingerprint_before"
  test "$(psql_value -c "select count(*) from import_batches where ingestion_mode='automatic'")" = "$automatic_batches_before"
  test "$(psql_value -c "select count(*) from users")" = "$users_before"
  echo "PHASE4A_E2E_LIGHT_REGRESSION_PASS market_prices=$(psql_value -c "select count(*) from market_prices") seat_positions=$(psql_value -c "select count(*) from seat_positions")"
  echo "PHASE4A_E2E_PASS"
  exit 0
fi

run_collector_with_peak() {
  local output=$1
  shift
  local peak=0 current
  "${COMPOSE[@]}" run --rm --no-deps --name "$CONTAINER_NAME" \
    collector --date "$COLLECTION_DATE" "$@" >"$output" 2>&1 &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    current=$(docker exec "$CONTAINER_NAME" sh -c \
      'if test -r /sys/fs/cgroup/memory.peak; then cat /sys/fs/cgroup/memory.peak; elif test -r /sys/fs/cgroup/memory/memory.max_usage_in_bytes; then cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes; fi' \
      2>/dev/null || true)
    if ! [[ "$current" =~ ^[0-9]+$ ]]; then
      current=$(docker stats --no-stream --format '{{.MemUsage}}' "$CONTAINER_NAME" \
        2>/dev/null | awk -F/ 'NR==1 {gsub(/ /,"",$1); print $1}' || true)
      current=$(numfmt --from=iec "$current" 2>/dev/null || echo 0)
    fi
    if [[ "$current" =~ ^[0-9]+$ ]]; then
      test "$current" -gt "$peak" && peak=$current
    fi
    sleep 1
  done
  local status=0
  wait "$pid" || status=$?
  printf '%s\n' "$peak" >"${output}.peak-bytes"
  return "$status"
}

# Deployment-acceptance exception only: DEC-041/HIGH-04 still requires the
# collector to fail and quarantine an incomplete DCE fallback dataset.  A
# partial Sina response may warn instead of blocking an unrelated release, but
# the production collector/import policy remains unchanged and writes no
# incomplete dataset to the projections.
declare -A DCE_INCOMPLETE_DATASETS=()
DCE_COMPLETENESS_WARNING_COUNT=0

accept_dce_completeness_only_failure() {
  local log=$1 run_label=$2 exchange dataset summary
  local missing_contracts skipped_items expected_items
  local -a dce_failed_datasets=() fallback_activated_datasets=()
  local -a incomplete_datasets=()

  for exchange in SHFE CZCE GFEX CFFEX; do
    if grep -Eq "^ERROR (exchange_failed|dataset_failed|dataset_submit_failed|batch_failed) exchange=$exchange([[:space:]]|$)" "$log"; then
      echo "PHASE4A_E2E_FAIL non_dce_failure run=$run_label exchange=$exchange" >&2
      return 1
    fi
    for dataset in futures_catalog_v1 trading_calendar_v1 daily_market_prices_v1 seat_positions_v1; do
      if ! grep -Eq "^INFO batch_succeeded exchange=$exchange dataset=$dataset([[:space:]]|$)" "$log"; then
        echo "PHASE4A_E2E_FAIL non_dce_success_missing run=$run_label exchange=$exchange dataset=$dataset" >&2
        return 1
      fi
    done
  done

  if grep -Eq '^ERROR (exchange_failed|dataset_submit_failed) exchange=DCE([[:space:]]|$)' "$log"; then
    echo "PHASE4A_E2E_FAIL dce_non_completeness_failure run=$run_label" >&2
    return 1
  fi
  if grep -E '^ERROR fallback_failed exchange=DCE ' "$log" |
    grep -Evq ' error=DatasetCompletenessError$'; then
    echo "PHASE4A_E2E_FAIL dce_fallback_unexpected_error run=$run_label" >&2
    return 1
  fi

  mapfile -t dce_failed_datasets < <(
    sed -nE 's/^ERROR dataset_failed exchange=DCE dataset=([^ ]+) error=[^ ]+$/\1/p' "$log" |
      sort -u
  )
  mapfile -t fallback_activated_datasets < <(
    sed -nE 's/^WARNING fallback_activated exchange=DCE dataset=([^ ]+) source=akshare_sina_dce_fallback$/\1/p' "$log" |
      sort -u
  )
  mapfile -t incomplete_datasets < <(
    sed -nE 's/^ERROR fallback_failed exchange=DCE dataset=([^ ]+) error=DatasetCompletenessError$/\1/p' "$log" |
      sort -u
  )
  test "${#incomplete_datasets[@]}" -gt 0 || {
    echo "PHASE4A_E2E_FAIL collector_failed_without_dce_completeness run=$run_label" >&2
    return 1
  }

  for dataset in "${dce_failed_datasets[@]}"; do
    if ! printf '%s\n' "${fallback_activated_datasets[@]}" | grep -Fxq "$dataset"; then
      echo "PHASE4A_E2E_FAIL dce_fallback_not_activated run=$run_label dataset=$dataset" >&2
      return 1
    fi
  done
  for dataset in "${fallback_activated_datasets[@]}"; do
    if grep -Eq "^INFO batch_succeeded exchange=DCE dataset=$dataset source=akshare_sina_dce_fallback([[:space:]]|$)" "$log"; then
      continue
    fi
    if ! printf '%s\n' "${incomplete_datasets[@]}" | grep -Fxq "$dataset"; then
      echo "PHASE4A_E2E_FAIL dce_fallback_terminal_result_missing run=$run_label dataset=$dataset" >&2
      return 1
    fi
  done

  for dataset in "${incomplete_datasets[@]}"; do
    if ! grep -Eq "^ERROR batch_failed exchange=DCE dataset=$dataset source=akshare_sina_dce_fallback reason=automatic_validation_failed$" "$log"; then
      echo "PHASE4A_E2E_FAIL dce_incomplete_batch_not_isolated run=$run_label dataset=$dataset" >&2
      return 1
    fi
    summary=$(awk -v target="$dataset" '
      function reset_attempt(    contract) {
        for (contract in missing) delete missing[contract]
        skipped = ""
        expected = ""
      }
      $0 == "WARNING fallback_activated exchange=DCE dataset=" target " source=akshare_sina_dce_fallback" {
        active = 1
        reset_attempt()
        next
      }
      active && $0 ~ ("^WARNING dataset_retry exchange=DCE dataset=" target " source=akshare_sina_dce_fallback ") {
        reset_attempt()
        next
      }
      active && $0 ~ /^WARNING dce_fallback_(market|seat)_contract_skipped contract=/ {
        for (field = 1; field <= NF; field++) {
          if ($field ~ /^contract=/) {
            split($field, pair, "=")
            missing[pair[2]] = 1
          }
        }
        next
      }
      active && $0 ~ /^ERROR dce_fallback_dataset_incomplete / {
        for (field = 1; field <= NF; field++) {
          if ($field ~ /^skipped_count=/) {
            split($field, pair, "=")
            skipped = pair[2]
          } else if ($field ~ /^expected_(contracts|requests)=/) {
            split($field, pair, "=")
            expected = pair[2]
          }
        }
        next
      }
      active && $0 == "ERROR fallback_failed exchange=DCE dataset=" target " error=DatasetCompletenessError" {
        count = 0
        for (contract in missing) count++
        print count "|" skipped "|" expected
        exit
      }
    ' "$log")
    IFS='|' read -r missing_contracts skipped_items expected_items <<<"$summary"
    if ! [[ "$missing_contracts" =~ ^[1-9][0-9]*$ ]] ||
      ! [[ "$skipped_items" =~ ^[1-9][0-9]*$ ]] ||
      ! [[ "$expected_items" =~ ^[1-9][0-9]*$ ]] ||
      test "$skipped_items" -ge "$expected_items"; then
      echo "PHASE4A_E2E_FAIL dce_fallback_empty_or_unstructured run=$run_label dataset=$dataset" >&2
      return 1
    fi
    DCE_INCOMPLETE_DATASETS["$dataset"]=1
    DCE_COMPLETENESS_WARNING_COUNT=$((DCE_COMPLETENESS_WARNING_COUNT + 1))
    echo "PHASE4A_E2E_WARNING dce_fallback_incomplete run=$run_label dataset=$dataset source=akshare_sina_dce_fallback missing_contracts=$missing_contracts missing_source_items=$skipped_items expected_source_items=$expected_items admission=non_blocking"
  done
}

echo "PHASE4A_E2E_STAGE first_run_started"
first_run_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
first_run_status=0
run_collector_with_peak "$EVIDENCE_DIR/first-run.log" || first_run_status=$?
if test "$first_run_status" -ne 0; then
  accept_dce_completeness_only_failure "$EVIDENCE_DIR/first-run.log" first
fi
echo "PHASE4A_E2E_STAGE first_run_completed"

workspace_id=$(psql_value -c \
  "select workspace_id from import_batches where ingestion_mode='automatic' and collection_date=date '$COLLECTION_DATE' order by created_at desc limit 1")
test -n "$workspace_id"

E2E_COLLECTOR_SESSION=$(new_uuid)
E2E_ADMIN_SESSION=$(new_uuid)
COLLECTOR_TOKEN=$(openssl rand -hex 32)
ADMIN_TOKEN=$(openssl rand -hex 32)
COLLECTOR_CSRF=$(openssl rand -hex 32)
ADMIN_CSRF=$(openssl rand -hex 32)
collector_token_hash=$(hash_token "$COLLECTOR_TOKEN")
admin_token_hash=$(hash_token "$ADMIN_TOKEN")
collector_csrf_hash=$(hash_token "$COLLECTOR_CSRF")
admin_csrf_hash=$(hash_token "$ADMIN_CSRF")
collector_user_id=$(psql_value -c "select id from users where username_normalized='collector-service' and disabled_at is null")
admin_user_id=$(psql_value -c "select owner_user_id from workspaces where id='$workspace_id'")
test -n "$collector_user_id"
test -n "$admin_user_id"
test "$(psql_value -c "select count(*) from user_roles where user_id='$admin_user_id' and role_name in ('admin','analyst')")" -ge 1
psql_value -c "insert into sessions(id,user_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at,user_agent) values ('$E2E_COLLECTOR_SESSION','$collector_user_id','$collector_token_hash','$collector_csrf_hash',now()+interval '2 hours',now()+interval '2 hours','phase4a-evaluator-fix-e2e'),('$E2E_ADMIN_SESSION','$admin_user_id','$admin_token_hash','$admin_csrf_hash',now()+interval '2 hours',now()+interval '2 hours','phase4a-evaluator-fix-e2e')" >/dev/null
unset collector_token_hash admin_token_hash collector_csrf_hash admin_csrf_hash

expected_official_sources=akshare_cffex_official,akshare_czce_official,akshare_gfex_official,akshare_shfe_official
for dataset in futures_catalog_v1 trading_calendar_v1 daily_market_prices_v1 seat_positions_v1; do
  actual_official_sources=$(psql_value -c \
    "select coalesce(string_agg(distinct source.code, ',' order by source.code), '') from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started' and source.code in ('akshare_cffex_official','akshare_czce_official','akshare_gfex_official','akshare_shfe_official')")
  test "$actual_official_sources" = "$expected_official_sources"
  dce_source_count=$(psql_value -c "select count(distinct source.code) from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback')")
  all_source_count=$(psql_value -c "select count(distinct source.code) from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started'")
  if test -n "${DCE_INCOMPLETE_DATASETS[$dataset]+set}"; then
    test "$dce_source_count" = 0
    test "$all_source_count" = 4
    test "$(psql_value -c "select count(*) from extraction_jobs job join data_sources source on source.workspace_id=job.workspace_id and source.id=job.data_source_id where job.workspace_id='$workspace_id' and source.code='akshare_sina_dce_fallback' and job.dataset_type='$dataset' and job.status='failed' and job.skipped_source_item_count>0 and job.started_at>=timestamptz '$first_run_started'")" -ge 1
  else
    test "$dce_source_count" = 1
    test "$all_source_count" = 5
  fi
done

test "$(psql_value -c "select count(*) from data_sources where workspace_id='$workspace_id' and code='akshare_sina_dce_fallback' and source_type='aggregator_public' and authorization_status='whitelisted_exception' and connector_code='akshare_v1'")" = 1
for dataset in futures_catalog_v1 trading_calendar_v1 daily_market_prices_v1 seat_positions_v1; do
  expected_dce_source_count=1
  test -n "${DCE_INCOMPLETE_DATASETS[$dataset]+set}" && expected_dce_source_count=0
  test "$(psql_value -c "select count(distinct source.code) from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback')")" = "$expected_dce_source_count"
done
dce_market_source=$(psql_value -c "select source.code from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='daily_market_prices_v1' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback') order by batch.created_at desc limit 1")
dce_seat_source=$(psql_value -c "select source.code from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='seat_positions_v1' and batch.status='succeeded' and batch.created_at>=timestamptz '$first_run_started' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback') order by batch.created_at desc limit 1")
case "$dce_market_source:$dce_seat_source" in
  akshare_dce_official:akshare_dce_official|akshare_sina_dce_fallback:akshare_sina_dce_fallback) ;;
  :|:akshare_dce_official|:akshare_sina_dce_fallback|akshare_dce_official:|akshare_sina_dce_fallback:) ;;
  *) echo "PHASE4A_E2E_FAIL inconsistent_dce_source" >&2; exit 1 ;;
esac

market_before=$(psql_value -c \
  "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
seats_before=$(psql_value -c \
  "select count(*) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
market_keys_before="$EVIDENCE_DIR/replay-market-keys-before.txt"
seat_keys_before="$EVIDENCE_DIR/replay-seat-keys-before.txt"
psql_value -c "select concat_ws('|',source_id,contract_id,trade_date,session_type,granularity,revision_no) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE' order by 1" >"$market_keys_before"
psql_value -c "select concat_ws('|',source_id,trade_date,contract_id,seat_id,rank_type,rank) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE' order by 1" >"$seat_keys_before"
test "$market_before" -gt 0
test "$seats_before" -gt 0
test "$(psql_value -c "select count(*) from exchanges where workspace_id='$workspace_id'")" -ge 5
test "$(psql_value -c "select count(*) from contracts where workspace_id='$workspace_id'")" -gt 0
if test -n "$dce_market_source"; then
  test "$(psql_value -c "select count(*) from market_prices price join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id join contracts contract on contract.workspace_id=price.workspace_id and contract.id=price.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where price.workspace_id='$workspace_id' and price.trade_date=date '$COLLECTION_DATE' and exchange.code='DCE' and source.code='$dce_market_source'")" -gt 0
fi
if test -n "$dce_seat_source"; then
  test "$(psql_value -c "select count(*) from seat_positions position join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id join contracts contract on contract.workspace_id=position.workspace_id and contract.id=position.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where position.workspace_id='$workspace_id' and position.trade_date=date '$COLLECTION_DATE' and exchange.code='DCE' and source.code='$dce_seat_source'")" -gt 0
fi

test "$(psql_value -c "select count(*) from (select workspace_id,source_id,contract_id,trade_date,session_type,granularity,revision_no,count(*) from market_prices where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0
test "$(psql_value -c "select count(*) from (select workspace_id,source_id,trade_date,contract_id,seat_id,rank_type,rank,count(*) from seat_positions where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0

echo "PHASE4A_E2E_STAGE replay_run_started"
replay_run_status=0
run_collector_with_peak "$EVIDENCE_DIR/replay-run.log" || replay_run_status=$?
if test "$replay_run_status" -ne 0; then
  accept_dce_completeness_only_failure "$EVIDENCE_DIR/replay-run.log" replay
fi
echo "PHASE4A_E2E_STAGE replay_run_completed"
market_after=$(psql_value -c \
  "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
seats_after=$(psql_value -c \
  "select count(*) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
market_keys_after="$EVIDENCE_DIR/replay-market-keys-after.txt"
seat_keys_after="$EVIDENCE_DIR/replay-seat-keys-after.txt"
psql_value -c "select concat_ws('|',source_id,contract_id,trade_date,session_type,granularity,revision_no) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE' order by 1" >"$market_keys_after"
psql_value -c "select concat_ws('|',source_id,trade_date,contract_id,seat_id,rank_type,rank) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE' order by 1" >"$seat_keys_after"
test -z "$(comm -23 "$market_keys_before" "$market_keys_after")"
test -z "$(comm -23 "$seat_keys_before" "$seat_keys_after")"
test "$(psql_value -c "select count(*) from (select workspace_id,source_id,contract_id,trade_date,session_type,granularity,revision_no,count(*) from market_prices where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0
test "$(psql_value -c "select count(*) from (select workspace_id,source_id,trade_date,contract_id,seat_id,rank_type,rank,count(*) from seat_positions where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0
echo "PHASE4A_E2E_REPLAY market_before=$market_before market_after=$market_after market_new=$((market_after-market_before)) seat_before=$seats_before seat_after=$seats_after seat_new=$((seats_after-seats_before))"

fault_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if run_collector_with_peak "$EVIDENCE_DIR/fault-run.log" \
  --dataset market --inject-failure-exchange DCE; then
  echo "PHASE4A_E2E_FAIL injected_failure_returned_success" >&2
  exit 1
fi
test "$(psql_value -c "select count(*) from extraction_jobs job join data_sources source on source.workspace_id=job.workspace_id and source.id=job.data_source_id where job.workspace_id='$workspace_id' and source.code='akshare_dce_official' and job.dataset_type='daily_market_prices_v1' and job.status='failed' and job.started_at >= timestamptz '$fault_started'")" -ge 1
test "$(psql_value -c "select coalesce(string_agg(distinct source.code, ',' order by source.code), '') from extraction_jobs job join data_sources source on source.workspace_id=job.workspace_id and source.id=job.data_source_id where job.workspace_id='$workspace_id' and job.dataset_type='daily_market_prices_v1' and job.status='succeeded' and job.started_at >= timestamptz '$fault_started'")" = "$expected_official_sources"
test "$(psql_value -c "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")" = "$market_before"

echo "PHASE4A_E2E_STAGE authorization_matrix_started"
RUN_MARK=$(new_uuid)
RUN_MARK=${RUN_MARK//-/}
RUN_MARK=${RUN_MARK:0:10}
RUN_MARK=${RUN_MARK^^}
UPSERT_INSTRUMENT="E2EU${RUN_MARK}"
UPSERT_CONTRACT="${UPSERT_INSTRUMENT}2608"
CFFEX_NAME=$(psql_value -c "select name from exchanges where workspace_id='$workspace_id' and code='CFFEX'")
CFFEX_TIMEZONE=$(psql_value -c "select timezone from exchanges where workspace_id='$workspace_id' and code='CFFEX'")
test -n "$CFFEX_NAME"
test -n "$CFFEX_TIMEZONE"
catalog_upsert_initial="$EVIDENCE_DIR/catalog-upsert-initial.csv"
catalog_upsert_filled="$EVIDENCE_DIR/catalog-upsert-filled.csv"
manual_probe="$EVIDENCE_DIR/manual-probe.csv"
printf '%s\n' 'date,code,name,value' "$COLLECTION_DATE,probe,probe,1" >"$manual_probe"
printf '%s\n' \
  'exchange_code,exchange_name,timezone,instrument_code,instrument_name,currency_code,contract_multiplier,price_tick,contract_code,delivery_month,listed_at,expires_at,source_record_ref' \
  "CFFEX,$CFFEX_NAME,$CFFEX_TIMEZONE,$UPSERT_INSTRUMENT,Phase 4A upsert,CNY,,,$UPSERT_CONTRACT,2026-08,,,e2e-upsert-initial-$RUN_MARK" \
  >"$catalog_upsert_initial"
printf '%s\n' \
  'exchange_code,exchange_name,timezone,instrument_code,instrument_name,currency_code,contract_multiplier,price_tick,contract_code,delivery_month,listed_at,expires_at,source_record_ref' \
  "CFFEX,$CFFEX_NAME,$CFFEX_TIMEZONE,$UPSERT_INSTRUMENT,Phase 4A upsert filled,CNY,10,0.2,$UPSERT_CONTRACT,2026-08,$COLLECTION_DATE,2099-12-31,e2e-upsert-filled-$RUN_MARK" \
  >"$catalog_upsert_filled"

assert_status "$(manual_upload "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" "$manual_probe" "$EVIDENCE_DIR/collector-manual-upload.json")" 403 'collector manual upload'
assert_status "$(automatic_upload "$ADMIN_TOKEN" "$ADMIN_CSRF" "$catalog_upsert_initial" futures_catalog_v1 akshare_cffex_official "$EVIDENCE_DIR/admin-automatic-upload.json")" 403 'admin automatic upload'

manual_import_id=$(psql_value -c "select id from import_batches where workspace_id='$workspace_id' and ingestion_mode='manual' order by created_at desc limit 1")
test -n "$manual_import_id"
DEFAULT_MAPPING='{"dataset_type":"generic","template_version_id":null,"fields":[{"source_column":"date","target_field":"trade_date","transform":"trim"},{"source_column":"code","target_field":"code","transform":"trim"},{"source_column":"name","target_field":"name","transform":"trim"},{"source_column":"value","target_field":"value","transform":"trim"}]}'
assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" POST "/api/v1/imports/$manual_import_id/inspect" '{}' "$EVIDENCE_DIR/matrix-collector-manual-inspect.json")" 403 'collector manual inspect'
assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" PUT "/api/v1/imports/$manual_import_id/mapping" "$DEFAULT_MAPPING" "$EVIDENCE_DIR/matrix-collector-manual-mapping.json")" 403 'collector manual mapping'
assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" POST "/api/v1/imports/$manual_import_id/preview" '{}' "$EVIDENCE_DIR/matrix-collector-manual-preview.json")" 403 'collector manual preview'
assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" POST "/api/v1/imports/$manual_import_id/validate" '{}' "$EVIDENCE_DIR/matrix-collector-manual-validate.json")" 403 'collector manual validate'
assert_status "$(api_json "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" POST "/api/v1/imports/$manual_import_id/confirm" '{"conflict_policy":"skip"}' "$EVIDENCE_DIR/matrix-collector-manual-confirm.json" "phase4a-matrix-manual-$RUN_MARK")" 403 'collector manual confirm'

upsert_initial_upload="$EVIDENCE_DIR/upsert-initial-upload.json"
assert_status "$(automatic_upload "$COLLECTOR_TOKEN" "$COLLECTOR_CSRF" "$catalog_upsert_initial" futures_catalog_v1 akshare_cffex_official "$upsert_initial_upload")" 201 'collector automatic upload'
upsert_initial_id=$(jq -r '.data.id' "$upsert_initial_upload")
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$upsert_initial_id/inspect" '{}' "$EVIDENCE_DIR/matrix-admin-auto-inspect.json")" 403 'admin automatic inspect'
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" PUT "/api/v1/imports/$upsert_initial_id/mapping" "$DEFAULT_MAPPING" "$EVIDENCE_DIR/matrix-admin-auto-mapping.json")" 403 'admin automatic mapping'
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$upsert_initial_id/preview" '{}' "$EVIDENCE_DIR/matrix-admin-auto-preview.json")" 403 'admin automatic preview'
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$upsert_initial_id/validate" '{}' "$EVIDENCE_DIR/matrix-admin-auto-validate.json")" 403 'admin automatic validate'
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$upsert_initial_id/confirm" '{"conflict_policy":"skip"}' "$EVIDENCE_DIR/matrix-admin-auto-confirm.json" "phase4a-matrix-auto-manual-$RUN_MARK")" 403 'admin automatic manual confirm'
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$upsert_initial_id/automatic-confirm" '{}' "$EVIDENCE_DIR/matrix-admin-auto-automatic-confirm.json" "phase4a-matrix-auto-$RUN_MARK")" 403 'admin automatic confirm'
confirm_automatic "$upsert_initial_id" upsert-initial
test "$(psql_value -c "select count(*) from import_mappings mapping cross join lateral jsonb_array_elements(mapping.mapping_json->'fields') field where mapping.workspace_id='$workspace_id' and mapping.import_batch_id='$upsert_initial_id' and field->>'source_column'=field->>'target_field' and coalesce(field->>'transform','')=''")" = 13
echo "PHASE4A_E2E_STAGE authorization_matrix_passed"

echo "PHASE4A_E2E_STAGE catalog_upsert_started"
upsert_version_before=$(psql_value -c "select contract.row_version from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$UPSERT_CONTRACT'")
test -n "$upsert_version_before"
upsert_filled_id=$(create_automatic_batch "$catalog_upsert_filled" futures_catalog_v1 akshare_cffex_official upsert-filled)
test "$(psql_value -c "select count(*) from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$UPSERT_CONTRACT' and contract.listed_at=date '$COLLECTION_DATE' and contract.expires_at=date '2099-12-31' and instrument.contract_multiplier=10 and instrument.price_tick=0.2 and contract.row_version>$upsert_version_before")" = 1
test "$(psql_value -c "select count(*) from import_row_changes where workspace_id='$workspace_id' and import_batch_id='$upsert_filled_id' and target_kind in ('instrument','contract') and operation='update' and before_json is not null and after_json is not null")" = 2
rollback_batch "$upsert_filled_id" upsert-filled
test "$(psql_value -c "select count(*) from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$UPSERT_CONTRACT' and contract.listed_at is null and contract.expires_at is null and instrument.contract_multiplier is null and instrument.price_tick is null and contract.row_version>$upsert_version_before")" = 1
echo "PHASE4A_E2E_STAGE catalog_upsert_passed"

echo "PHASE4A_E2E_STAGE projection_rollback_started"
FIRST_EXCHANGE_BATCH=$(new_uuid)
FIRST_EXCHANGE_FILE=$(new_uuid)
FIRST_EXCHANGE_RECORD=$(new_uuid)
FIRST_EXCHANGE_CHANGE_RECORD=$(new_uuid)
FIRST_EXCHANGE_CHANGE_PROJECTION=$(new_uuid)
FIRST_EXCHANGE_ID=$(new_uuid)
FIRST_EXCHANGE_CODE="E2E${RUN_MARK//-/}"
FIRST_CALENDAR_BATCH=$(new_uuid)
FIRST_CALENDAR_FILE=$(new_uuid)
FIRST_CALENDAR_RECORD=$(new_uuid)
FIRST_CALENDAR_CHANGE_RECORD=$(new_uuid)
FIRST_CALENDAR_CHANGE_PROJECTION=$(new_uuid)
FIRST_CALENDAR_ID=$(new_uuid)
FIRST_CALENDAR_VERSION="phase4a-exchange-dependency-$RUN_MARK"
CFFEX_SOURCE_ID=$(psql_value -c "select id from data_sources where workspace_id='$workspace_id' and code='akshare_cffex_official'")

# Isolated v2 audit fixtures model the first catalog batch that creates an exchange
# and a later calendar batch that owns the dependent calendar version. Both batches
# are rolled back through the public API below, so only their immutable audit trail remains.
psql_value -c "begin;
insert into import_batches
  (id,workspace_id,status,created_by,dataset_type,committed_at,imported_count,
   rollback_capability,change_log_version,ingestion_mode,data_source_id,collection_date,
   fixed_template_code)
values
  ('$FIRST_EXCHANGE_BATCH','$workspace_id','succeeded','$admin_user_id','futures_catalog_v1',
   now(),1,'direct',2,'automatic','$CFFEX_SOURCE_ID',date '$COLLECTION_DATE',
   'futures_catalog_v1@1');
insert into import_files
  (id,workspace_id,import_batch_id,stored_object_id,original_filename,declared_mime_type,
   detected_format,sha256,size_bytes,created_by)
select '$FIRST_EXCHANGE_FILE',workspace_id,'$FIRST_EXCHANGE_BATCH',stored_object_id,
       'phase4a-first-exchange.csv',declared_mime_type,detected_format,sha256,size_bytes,
       '$admin_user_id'
  from import_files where workspace_id='$workspace_id' order by created_at limit 1;
insert into imported_records
  (id,workspace_id,dataset_type,business_key,record_data,source_import_batch_id,
   source_row_number,created_by)
values
  ('$FIRST_EXCHANGE_RECORD','$workspace_id','futures_catalog_v1',
   'AKSHARE_CFFEX_OFFICIAL|$FIRST_EXCHANGE_CODE','{}'::jsonb,
   '$FIRST_EXCHANGE_BATCH',1,'$admin_user_id');
insert into exchanges
  (id,workspace_id,code,name,timezone,source_record_id)
values
  ('$FIRST_EXCHANGE_ID','$workspace_id','$FIRST_EXCHANGE_CODE',
   'Phase 4A exchange dependency fixture','Asia/Shanghai','$FIRST_EXCHANGE_RECORD');
insert into import_row_changes
  (id,workspace_id,import_batch_id,sequence_no,target_kind,target_id,operation,before_json,
   after_json,target_row_version,source_file_id,source_row_number)
select '$FIRST_EXCHANGE_CHANGE_RECORD','$workspace_id','$FIRST_EXCHANGE_BATCH',1,
       'imported_record','$FIRST_EXCHANGE_RECORD','insert',null,
       jsonb_build_object('record_data',record_data,'source_import_batch_id',source_import_batch_id,
                          'source_row_number',source_row_number,'row_version',row_version),
       row_version,'$FIRST_EXCHANGE_FILE',1
  from imported_records where workspace_id='$workspace_id' and id='$FIRST_EXCHANGE_RECORD';
insert into import_row_changes
  (id,workspace_id,import_batch_id,sequence_no,target_kind,target_id,operation,before_json,
   after_json,target_row_version,source_file_id,source_row_number)
select '$FIRST_EXCHANGE_CHANGE_PROJECTION','$workspace_id','$FIRST_EXCHANGE_BATCH',2,
       'exchange','$FIRST_EXCHANGE_ID','insert',null,
       to_jsonb(exchange_row)-'workspace_id'-'created_at'-'updated_at',row_version,
       '$FIRST_EXCHANGE_FILE',1
  from exchanges exchange_row where workspace_id='$workspace_id' and id='$FIRST_EXCHANGE_ID';
commit;" >/dev/null

psql_value -c "begin;
insert into import_batches
  (id,workspace_id,status,created_by,dataset_type,committed_at,imported_count,
   rollback_capability,change_log_version,ingestion_mode,data_source_id,collection_date,
   fixed_template_code)
values
  ('$FIRST_CALENDAR_BATCH','$workspace_id','succeeded','$admin_user_id','trading_calendar_v1',
   now(),1,'direct',2,'automatic','$CFFEX_SOURCE_ID',date '$COLLECTION_DATE',
   'trading_calendar_v1@1');
insert into import_files
  (id,workspace_id,import_batch_id,stored_object_id,original_filename,declared_mime_type,
   detected_format,sha256,size_bytes,created_by)
select '$FIRST_CALENDAR_FILE',workspace_id,'$FIRST_CALENDAR_BATCH',stored_object_id,
       'phase4a-first-exchange-calendar.csv',declared_mime_type,detected_format,sha256,
       size_bytes,'$admin_user_id'
  from import_files where workspace_id='$workspace_id' order by created_at limit 1;
insert into imported_records
  (id,workspace_id,dataset_type,business_key,record_data,source_import_batch_id,
   source_row_number,created_by)
values
  ('$FIRST_CALENDAR_RECORD','$workspace_id','trading_calendar_v1',
   'AKSHARE_CFFEX_OFFICIAL|$FIRST_EXCHANGE_CODE|$FIRST_CALENDAR_VERSION','{}'::jsonb,
   '$FIRST_CALENDAR_BATCH',1,'$admin_user_id');
insert into trading_calendar_versions
  (id,workspace_id,exchange_id,version,source_id,effective_from,created_by,source_record_id)
values
  ('$FIRST_CALENDAR_ID','$workspace_id','$FIRST_EXCHANGE_ID','$FIRST_CALENDAR_VERSION',
   '$CFFEX_SOURCE_ID',date '$COLLECTION_DATE','$admin_user_id','$FIRST_CALENDAR_RECORD');
insert into import_row_changes
  (id,workspace_id,import_batch_id,sequence_no,target_kind,target_id,operation,before_json,
   after_json,target_row_version,source_file_id,source_row_number)
select '$FIRST_CALENDAR_CHANGE_RECORD','$workspace_id','$FIRST_CALENDAR_BATCH',1,
       'imported_record','$FIRST_CALENDAR_RECORD','insert',null,
       jsonb_build_object('record_data',record_data,'source_import_batch_id',source_import_batch_id,
                          'source_row_number',source_row_number,'row_version',row_version),
       row_version,'$FIRST_CALENDAR_FILE',1
  from imported_records where workspace_id='$workspace_id' and id='$FIRST_CALENDAR_RECORD';
insert into import_row_changes
  (id,workspace_id,import_batch_id,sequence_no,target_kind,target_id,operation,before_json,
   after_json,target_row_version,source_file_id,source_row_number)
select '$FIRST_CALENDAR_CHANGE_PROJECTION','$workspace_id','$FIRST_CALENDAR_BATCH',2,
       'trading_calendar_version','$FIRST_CALENDAR_ID','insert',null,
       to_jsonb(calendar_row)-'workspace_id'-'created_at',row_version,
       '$FIRST_CALENDAR_FILE',1
  from trading_calendar_versions calendar_row
 where workspace_id='$workspace_id' and id='$FIRST_CALENDAR_ID';
commit;" >/dev/null

first_exchange_snapshot=$(psql_value -c "select md5((select to_jsonb(exchange_row)::text from exchanges exchange_row where workspace_id='$workspace_id' and id='$FIRST_EXCHANGE_ID') || (select to_jsonb(calendar_row)::text from trading_calendar_versions calendar_row where workspace_id='$workspace_id' and id='$FIRST_CALENDAR_ID'))")
first_exchange_check="$EVIDENCE_DIR/first-exchange-dependency-check.json"
assert_status "$(rollback_check "$FIRST_EXCHANGE_BATCH" "$first_exchange_check")" 200 "first exchange dependency precheck"
jq -e --arg exchange_id "$FIRST_EXCHANGE_ID" '
  .data.can_rollback == false
  and (.data.conflicts | any(
    .conflict_type == "downstream_dependency"
    and .target_kind == "exchange"
    and .target_id == $exchange_id
    and .dependency_kind == "trading_calendar_version"
    and .detail_code == "formal_projection_downstream_dependency"
  ))
' "$first_exchange_check" >/dev/null
first_exchange_request=$(jq -r '.data.precheck_request_id' "$first_exchange_check")
first_exchange_fingerprint=$(jq -r '.data.precheck_fingerprint' "$first_exchange_check")
assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$FIRST_EXCHANGE_BATCH/rollback" "{\"precheck_request_id\":\"$first_exchange_request\",\"precheck_fingerprint\":\"$first_exchange_fingerprint\"}" "$EVIDENCE_DIR/first-exchange-dependency-rollback.json" "phase4a-first-exchange-conflict-$RUN_MARK")" 409 "first exchange dependency rollback"
test "$(psql_value -c "select md5((select to_jsonb(exchange_row)::text from exchanges exchange_row where workspace_id='$workspace_id' and id='$FIRST_EXCHANGE_ID') || (select to_jsonb(calendar_row)::text from trading_calendar_versions calendar_row where workspace_id='$workspace_id' and id='$FIRST_CALENDAR_ID'))")" = "$first_exchange_snapshot"
rollback_batch "$FIRST_CALENDAR_BATCH" first-exchange-calendar
rollback_batch "$FIRST_EXCHANGE_BATCH" first-exchange-catalog
test "$(psql_value -c "select count(*) from exchanges where workspace_id='$workspace_id' and id='$FIRST_EXCHANGE_ID'")" = 0
test "$(psql_value -c "select count(*) from trading_calendar_versions where workspace_id='$workspace_id' and id='$FIRST_CALENDAR_ID'")" = 0

ROLL_INSTRUMENT="E2ER${RUN_MARK}"
ROLL_CONTRACT="${ROLL_INSTRUMENT}2609"
ROLL_CALENDAR="phase4a-e2e-cffex-$RUN_MARK"
ROLL_SEAT="Phase4A E2E Seat $RUN_MARK"
roll_catalog_file="$EVIDENCE_DIR/rollback-catalog.csv"
roll_calendar_file="$EVIDENCE_DIR/rollback-calendar.csv"
roll_market_file="$EVIDENCE_DIR/rollback-market.csv"
roll_seat_file="$EVIDENCE_DIR/rollback-seat.csv"
printf '%s\n' \
  'exchange_code,exchange_name,timezone,instrument_code,instrument_name,currency_code,contract_multiplier,price_tick,contract_code,delivery_month,listed_at,expires_at,source_record_ref' \
  "CFFEX,$CFFEX_NAME,$CFFEX_TIMEZONE,$ROLL_INSTRUMENT,Phase 4A rollback,CNY,20,0.4,$ROLL_CONTRACT,2026-09,$COLLECTION_DATE,2099-12-31,e2e-rollback-catalog-$RUN_MARK" \
  >"$roll_catalog_file"
printf '%s\n' \
  'exchange_code,calendar_version,effective_from,trade_date,is_trading_day,day_session_json,night_session_json,source_record_ref' \
  "CFFEX,$ROLL_CALENDAR,$COLLECTION_DATE,$COLLECTION_DATE,true,{},{},e2e-rollback-calendar-$RUN_MARK" \
  >"$roll_calendar_file"
OBSERVED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' \
  'exchange_code,contract_code,trade_date,session_type,observed_at,granularity,close_price,settlement_price,currency_code,calendar_version,revision_no,source_record_ref' \
  "CFFEX,$ROLL_CONTRACT,$COLLECTION_DATE,day,$OBSERVED_AT,daily,101.25,101.00,CNY,$ROLL_CALENDAR,700001,e2e-rollback-market-$RUN_MARK" \
  >"$roll_market_file"
printf '%s\n' \
  'exchange_code,contract_code,trade_date,seat_name,rank_type,rank,volume,long_position,short_position,source_record_ref' \
  "CFFEX,$ROLL_CONTRACT,$COLLECTION_DATE,$ROLL_SEAT,volume,700001,100,,,e2e-rollback-seat-$RUN_MARK" \
  >"$roll_seat_file"

roll_catalog_id=$(create_automatic_batch "$roll_catalog_file" futures_catalog_v1 akshare_cffex_official rollback-catalog)
roll_calendar_id=$(create_automatic_batch "$roll_calendar_file" trading_calendar_v1 akshare_cffex_official rollback-calendar)
OBSERVED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' \
  'exchange_code,contract_code,trade_date,session_type,observed_at,granularity,close_price,settlement_price,currency_code,calendar_version,revision_no,source_record_ref' \
  "CFFEX,$ROLL_CONTRACT,$COLLECTION_DATE,daily,$OBSERVED_AT,1d,101.25,101.00,CNY,$ROLL_CALENDAR,700001,e2e-rollback-market-$RUN_MARK" \
  >"$roll_market_file"
roll_market_id=$(create_automatic_batch "$roll_market_file" daily_market_prices_v1 akshare_cffex_official rollback-market)
roll_seat_id=$(create_automatic_batch "$roll_seat_file" seat_positions_v1 akshare_cffex_official rollback-seat)
test "$(psql_value -c "select string_agg(target_kind, ',' order by target_kind) from import_row_changes where workspace_id='$workspace_id' and import_batch_id='$roll_catalog_id' and target_kind<>'imported_record' and operation='insert'")" = "contract,instrument"
test "$(psql_value -c "select string_agg(target_kind, ',' order by target_kind) from import_row_changes where workspace_id='$workspace_id' and import_batch_id='$roll_calendar_id' and target_kind<>'imported_record' and operation='insert'")" = "trading_calendar_day,trading_calendar_version"
test "$(psql_value -c "select string_agg(target_kind, ',' order by target_kind) from import_row_changes where workspace_id='$workspace_id' and import_batch_id='$roll_market_id' and target_kind<>'imported_record' and operation='insert'")" = "market_price"
test "$(psql_value -c "select string_agg(target_kind, ',' order by target_kind) from import_row_changes where workspace_id='$workspace_id' and import_batch_id='$roll_seat_id' and target_kind<>'imported_record' and operation='insert'")" = "seat_entity,seat_position"
test "$(psql_value -c "select count(*) from market_prices price join import_batches batch on batch.workspace_id=price.workspace_id and batch.id=price.source_import_batch_id where price.workspace_id='$workspace_id' and batch.id='$roll_market_id' and price.observed_at>=timestamptz '$OBSERVED_AT' and price.observed_at<=batch.committed_at")" = 1

formal_snapshot_before_conflict=$(psql_value -c "select md5((select to_jsonb(contract)::text from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$ROLL_CONTRACT') || (select to_jsonb(version)::text from trading_calendar_versions version join exchanges exchange on exchange.workspace_id=version.workspace_id and exchange.id=version.exchange_id where version.workspace_id='$workspace_id' and exchange.code='CFFEX' and version.version='$ROLL_CALENDAR') || (select to_jsonb(price)::text from market_prices price where price.workspace_id='$workspace_id' and price.source_import_batch_id='$roll_market_id') || (select to_jsonb(position)::text from seat_positions position where position.workspace_id='$workspace_id' and position.source_import_batch_id='$roll_seat_id'))")
for dependency_spec in "catalog:$roll_catalog_id" "calendar:$roll_calendar_id"; do
  dependency_label=${dependency_spec%%:*}
  dependency_id=${dependency_spec##*:}
  dependency_check="$EVIDENCE_DIR/$dependency_label-dependency-check.json"
  assert_status "$(rollback_check "$dependency_id" "$dependency_check")" 200 "$dependency_label dependency precheck"
  jq -e '.data.can_rollback == false and .data.conflict_count > 0' "$dependency_check" >/dev/null
  dependency_request=$(jq -r '.data.precheck_request_id' "$dependency_check")
  dependency_fingerprint=$(jq -r '.data.precheck_fingerprint' "$dependency_check")
  assert_status "$(api_json "$ADMIN_TOKEN" "$ADMIN_CSRF" POST "/api/v1/imports/$dependency_id/rollback" "{\"precheck_request_id\":\"$dependency_request\",\"precheck_fingerprint\":\"$dependency_fingerprint\"}" "$EVIDENCE_DIR/$dependency_label-dependency-rollback.json" "phase4a-$dependency_label-conflict-$RUN_MARK")" 409 "$dependency_label dependency rollback"
done
test "$(psql_value -c "select md5((select to_jsonb(contract)::text from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$ROLL_CONTRACT') || (select to_jsonb(version)::text from trading_calendar_versions version join exchanges exchange on exchange.workspace_id=version.workspace_id and exchange.id=version.exchange_id where version.workspace_id='$workspace_id' and exchange.code='CFFEX' and version.version='$ROLL_CALENDAR') || (select to_jsonb(price)::text from market_prices price where price.workspace_id='$workspace_id' and price.source_import_batch_id='$roll_market_id') || (select to_jsonb(position)::text from seat_positions position where position.workspace_id='$workspace_id' and position.source_import_batch_id='$roll_seat_id'))")" = "$formal_snapshot_before_conflict"

rollback_batch "$roll_seat_id" rollback-seat
rollback_batch "$roll_market_id" rollback-market
rollback_batch "$roll_calendar_id" rollback-calendar
rollback_batch "$roll_catalog_id" rollback-catalog
test "$(psql_value -c "select count(*) from market_prices where workspace_id='$workspace_id' and source_import_batch_id='$roll_market_id'")" = 0
test "$(psql_value -c "select count(*) from seat_positions where workspace_id='$workspace_id' and source_import_batch_id='$roll_seat_id'")" = 0
test "$(psql_value -c "select count(*) from trading_calendar_versions version join exchanges exchange on exchange.workspace_id=version.workspace_id and exchange.id=version.exchange_id where version.workspace_id='$workspace_id' and exchange.code='CFFEX' and version.version='$ROLL_CALENDAR'")" = 0
test "$(psql_value -c "select count(*) from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='CFFEX' and contract.code='$ROLL_CONTRACT'")" = 0
echo "PHASE4A_E2E_STAGE projection_rollback_passed"

echo "PHASE4A_E2E_STAGE dce_source_recovery_started"
DCE_CONTRACT=$(psql_value -c "select contract.code from contracts contract join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where contract.workspace_id='$workspace_id' and exchange.code='DCE' order by contract.code limit 1")
test -n "$DCE_CONTRACT"
DCE_CALENDAR="phase4a-e2e-dce-$RUN_MARK"
DCE_SEAT="Phase4A Source Seat $RUN_MARK"
dce_fallback_calendar_file="$EVIDENCE_DIR/dce-fallback-calendar.csv"
dce_official_calendar_file="$EVIDENCE_DIR/dce-official-calendar.csv"
printf '%s\n' \
  'exchange_code,calendar_version,effective_from,trade_date,is_trading_day,day_session_json,night_session_json,source_record_ref' \
  "DCE,$DCE_CALENDAR,$COLLECTION_DATE,$COLLECTION_DATE,true,{},{},e2e-dce-fallback-calendar-$RUN_MARK" \
  >"$dce_fallback_calendar_file"
printf '%s\n' \
  'exchange_code,calendar_version,effective_from,trade_date,is_trading_day,day_session_json,night_session_json,source_record_ref' \
  "DCE,$DCE_CALENDAR,$COLLECTION_DATE,$COLLECTION_DATE,true,{},{},e2e-dce-official-calendar-$RUN_MARK" \
  >"$dce_official_calendar_file"
dce_fallback_calendar_id=$(create_automatic_batch "$dce_fallback_calendar_file" trading_calendar_v1 akshare_sina_dce_fallback dce-fallback-calendar)
dce_official_calendar_id=$(create_automatic_batch "$dce_official_calendar_file" trading_calendar_v1 akshare_dce_official dce-official-calendar)
test "$(psql_value -c "select count(*) from trading_calendar_versions version join exchanges exchange on exchange.workspace_id=version.workspace_id and exchange.id=version.exchange_id join data_sources source on source.workspace_id=version.workspace_id and source.id=version.source_id where version.workspace_id='$workspace_id' and exchange.code='DCE' and version.version='$DCE_CALENDAR' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback')")" = 2

declare -a dce_fact_batch_ids=()
for scenario in same different; do
  if test "$scenario" = same; then
    revision=800001
    fallback_price=211.50
    official_price=211.50
    rank=800001
    fallback_volume=211
    official_volume=211
  else
    revision=800002
    fallback_price=221.50
    official_price=222.50
    rank=800002
    fallback_volume=221
    official_volume=222
  fi
  for source_kind in fallback official; do
    if test "$source_kind" = fallback; then
      source_code=akshare_sina_dce_fallback
      price=$fallback_price
      volume=$fallback_volume
    else
      source_code=akshare_dce_official
      price=$official_price
      volume=$official_volume
    fi
    market_file="$EVIDENCE_DIR/dce-$scenario-$source_kind-market.csv"
    seat_file="$EVIDENCE_DIR/dce-$scenario-$source_kind-seat.csv"
    printf '%s\n' \
      'exchange_code,contract_code,trade_date,session_type,observed_at,granularity,close_price,settlement_price,currency_code,calendar_version,revision_no,source_record_ref' \
      "DCE,$DCE_CONTRACT,$COLLECTION_DATE,daily,$OBSERVED_AT,1d,$price,$price,CNY,$DCE_CALENDAR,$revision,e2e-dce-$scenario-$source_kind-market-$RUN_MARK" \
      >"$market_file"
    printf '%s\n' \
      'exchange_code,contract_code,trade_date,seat_name,rank_type,rank,volume,long_position,short_position,source_record_ref' \
      "DCE,$DCE_CONTRACT,$COLLECTION_DATE,$DCE_SEAT,volume,$rank,$volume,,,e2e-dce-$scenario-$source_kind-seat-$RUN_MARK" \
      >"$seat_file"
    market_batch=$(create_automatic_batch "$market_file" daily_market_prices_v1 "$source_code" "dce-$scenario-$source_kind-market")
    seat_batch=$(create_automatic_batch "$seat_file" seat_positions_v1 "$source_code" "dce-$scenario-$source_kind-seat")
    dce_fact_batch_ids+=("$market_batch" "$seat_batch")
  done
done

test "$(psql_value -c "select count(*) from market_prices price join contracts contract on contract.workspace_id=price.workspace_id and contract.id=price.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id where price.workspace_id='$workspace_id' and exchange.code='DCE' and contract.code='$DCE_CONTRACT' and price.trade_date=date '$COLLECTION_DATE' and price.revision_no in (800001,800002) and source.code in ('akshare_dce_official','akshare_sina_dce_fallback')")" = 4
test "$(psql_value -c "select count(*) from seat_positions position join contracts contract on contract.workspace_id=position.workspace_id and contract.id=position.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id join seat_entities seat on seat.workspace_id=position.workspace_id and seat.id=position.seat_id where position.workspace_id='$workspace_id' and exchange.code='DCE' and contract.code='$DCE_CONTRACT' and position.trade_date=date '$COLLECTION_DATE' and position.rank in (800001,800002) and seat.canonical_name='$DCE_SEAT' and source.code in ('akshare_dce_official','akshare_sina_dce_fallback')")" = 4
test "$(psql_value -c "select count(*) from imported_records record join import_batches batch on batch.workspace_id=record.workspace_id and batch.id=record.source_import_batch_id join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where record.workspace_id='$workspace_id' and batch.id in ('${dce_fact_batch_ids[0]}','${dce_fact_batch_ids[1]}','${dce_fact_batch_ids[2]}','${dce_fact_batch_ids[3]}','${dce_fact_batch_ids[4]}','${dce_fact_batch_ids[5]}','${dce_fact_batch_ids[6]}','${dce_fact_batch_ids[7]}') and record.business_key like upper(source.code)||'|%'")" = 8
test "$(psql_value -c "select source.code from preferred_market_prices price join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id join contracts contract on contract.workspace_id=price.workspace_id and contract.id=price.contract_id where price.workspace_id='$workspace_id' and contract.code='$DCE_CONTRACT' and price.trade_date=date '$COLLECTION_DATE' and price.revision_no=800001")" = akshare_dce_official
test "$(psql_value -c "select count(*) from preferred_market_prices price join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id join contracts contract on contract.workspace_id=price.workspace_id and contract.id=price.contract_id where price.workspace_id='$workspace_id' and contract.code='$DCE_CONTRACT' and price.trade_date=date '$COLLECTION_DATE' and price.revision_no=800002 and source.code='akshare_dce_official' and price.close_price=222.50")" = 1
test "$(psql_value -c "select source.code from preferred_seat_positions position join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id join contracts contract on contract.workspace_id=position.workspace_id and contract.id=position.contract_id join seat_entities seat on seat.workspace_id=position.workspace_id and seat.id=position.seat_id where position.workspace_id='$workspace_id' and contract.code='$DCE_CONTRACT' and position.trade_date=date '$COLLECTION_DATE' and position.rank=800001 and seat.canonical_name='$DCE_SEAT'")" = akshare_dce_official
test "$(psql_value -c "select source.code||':'||position.volume from preferred_seat_positions position join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id join contracts contract on contract.workspace_id=position.workspace_id and contract.id=position.contract_id join seat_entities seat on seat.workspace_id=position.workspace_id and seat.id=position.seat_id where position.workspace_id='$workspace_id' and contract.code='$DCE_CONTRACT' and position.trade_date=date '$COLLECTION_DATE' and position.rank=800002 and seat.canonical_name='$DCE_SEAT'")" = akshare_dce_official:222
test "$(psql_value -c "select (select priority from data_sources where workspace_id='$workspace_id' and code='akshare_dce_official') > (select priority from data_sources where workspace_id='$workspace_id' and code='akshare_sina_dce_fallback')")" = t

for index in 7 6 5 4 3 2 1 0; do
  rollback_batch "${dce_fact_batch_ids[$index]}" "dce-fact-$index"
done
rollback_batch "$dce_official_calendar_id" dce-official-calendar
rollback_batch "$dce_fallback_calendar_id" dce-fallback-calendar
test "$(psql_value -c "select count(*) from market_prices where workspace_id='$workspace_id' and source_import_batch_id in ('${dce_fact_batch_ids[0]}','${dce_fact_batch_ids[2]}','${dce_fact_batch_ids[4]}','${dce_fact_batch_ids[6]}')")" = 0
test "$(psql_value -c "select count(*) from seat_positions where workspace_id='$workspace_id' and source_import_batch_id in ('${dce_fact_batch_ids[1]}','${dce_fact_batch_ids[3]}','${dce_fact_batch_ids[5]}','${dce_fact_batch_ids[7]}')")" = 0
echo "PHASE4A_E2E_STAGE dce_source_recovery_passed"

test "$(psql_value -c "select count(*) from market_prices price join imported_records record on record.workspace_id=price.workspace_id and record.id=price.source_record_id join import_batches batch on batch.workspace_id=price.workspace_id and batch.id=price.source_import_batch_id where price.workspace_id='$workspace_id' and batch.ingestion_mode='automatic' and record.source_import_batch_id=batch.id")" = "$(psql_value -c "select count(*) from market_prices where workspace_id='$workspace_id'")"

rls_visible=$(psql_value -c \
  "begin; set local role futures_runtime; select set_config('app.current_workspace_id','00000000-0000-0000-0000-000000000000',true); select count(*) from market_prices; rollback")
test "$(printf '%s\n' "$rls_visible" | tail -n1)" = 0

peak_first=$(cat "$EVIDENCE_DIR/first-run.log.peak-bytes")
peak_replay=$(cat "$EVIDENCE_DIR/replay-run.log.peak-bytes")
peak_fault=$(cat "$EVIDENCE_DIR/fault-run.log.peak-bytes")
peak_bytes=$peak_first
test "$peak_replay" -gt "$peak_bytes" && peak_bytes=$peak_replay
test "$peak_fault" -gt "$peak_bytes" && peak_bytes=$peak_fault
echo "PHASE4A_E2E_MEMORY peak_bytes=$peak_bytes limit_bytes=536870912"
test "$peak_bytes" -gt 0
test "$peak_bytes" -le 536870912
test "$(psql_value -c "select count(*) from import_batches where ingestion_mode='manual'")" = "$legacy_batches_before"
test "$(psql_value -c "select md5(coalesce(string_agg(to_jsonb(batch)::text, '|' order by id), '')) from import_batches batch where ingestion_mode='manual'")" = "$legacy_batches_fingerprint_before"
test "$(psql_value -c "select count(*) from users")" = "$users_before"
test "$(psql_value -c "select md5(coalesce(string_agg((to_jsonb(app_user) - 'updated_at' - 'last_login_at')::text, '|' order by id), '')) from users app_user")" = "$users_identity_fingerprint_before"

if grep -Eiq \
  'authorization:[[:space:]]*bearer|set-cookie:|"password"|collector-credentials|csrf_token' \
  "$EVIDENCE_DIR"/*.log; then
  echo "PHASE4A_E2E_FAIL credential_pattern_in_collector_log" >&2
  exit 1
fi

{
  echo "collection_date=$COLLECTION_DATE"
  echo "workspace_id=$workspace_id"
  echo "market_prices=$market_before"
  echo "seat_positions=$seats_before"
  echo "collector_peak_bytes=$peak_bytes"
  echo "legacy_import_batches=$legacy_batches_before"
  echo "users_unchanged=PASS"
  echo "cron=PASS"
  echo "idempotent_replay=PASS"
  echo "source_failure_isolation=PASS"
  echo "rls=PASS"
  echo "provenance=PASS"
  echo "dce_source_provenance=PASS"
  if test "$DCE_COMPLETENESS_WARNING_COUNT" -gt 0; then
    echo "exact_source_sets=PASS_WITH_DCE_COMPLETENESS_WARNING"
    echo "dce_completeness_warnings=$DCE_COMPLETENESS_WARNING_COUNT"
  else
    echo "exact_source_sets=PASS"
    echo "dce_completeness_warnings=0"
  fi
  echo "identity_mode_matrix=PASS"
  echo "automatic_fixed_pipeline=PASS"
  echo "projection_rollback=PASS"
  echo "rollback_dependency_and_stale_conflicts=PASS"
  echo "catalog_upsert_and_restore=PASS"
  echo "dce_source_recovery_same_and_different=PASS"
  echo "collector_work_tmpfs=PASS"
} >"$EVIDENCE_DIR/result.env"
chmod 600 "$EVIDENCE_DIR/result.env"
echo PHASE4A_E2E_PASS
