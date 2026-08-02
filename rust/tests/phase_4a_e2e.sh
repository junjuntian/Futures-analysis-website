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

psql_value() {
  "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -X -U "$DB_USER" -d "$DB_NAME" -Atq -v ON_ERROR_STOP=1 "$@"
}

legacy_batches_before=$(psql_value -c \
  "select count(*) from import_batches where ingestion_mode='manual'")
legacy_batches_fingerprint_before=$(psql_value -c \
  "select md5(coalesce(string_agg(to_jsonb(batch)::text, '|' order by id), '')) from import_batches batch where ingestion_mode='manual'")
automatic_batches_before=$(psql_value -c \
  "select count(*) from import_batches where ingestion_mode='automatic'")
users_before=$(psql_value -c "select count(*) from users")
users_fingerprint_before=$(psql_value -c \
  "select md5(coalesce(string_agg(to_jsonb(app_user)::text, '|' order by id), '')) from users app_user")
echo "PHASE4A_E2E_BASELINE manual_batches=$legacy_batches_before automatic_batches=$automatic_batches_before users=$users_before"
test "$legacy_batches_before" -ge 127
echo "PHASE4A_E2E_STAGE baseline_counts_passed"

run_collector_with_peak() {
  local output=$1
  shift
  local peak=0 current
  "${COMPOSE[@]}" run --rm --no-deps --name "$CONTAINER_NAME" \
    collector --date "$COLLECTION_DATE" "$@" >"$output" 2>&1 &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    current=$(docker stats --no-stream --format '{{.MemUsage}}' "$CONTAINER_NAME" \
      2>/dev/null | awk -F/ 'NR==1 {gsub(/ /,"",$1); print $1}' || true)
    if test -n "$current"; then
      current=$(numfmt --from=iec "$current" 2>/dev/null || echo 0)
      test "$current" -gt "$peak" && peak=$current
    fi
    sleep 1
  done
  wait "$pid"
  printf '%s\n' "$peak" >"${output}.peak-bytes"
}

echo "PHASE4A_E2E_STAGE first_run_started"
run_collector_with_peak "$EVIDENCE_DIR/first-run.log"
echo "PHASE4A_E2E_STAGE first_run_completed"

workspace_id=$(psql_value -c \
  "select workspace_id from import_batches where ingestion_mode='automatic' and collection_date=date '$COLLECTION_DATE' order by created_at desc limit 1")
test -n "$workspace_id"

for dataset in daily_market_prices_v1 seat_positions_v1; do
  succeeded=$(psql_value -c \
    "select count(distinct source.code) from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded'")
  test "$succeeded" = 5
done

test "$(psql_value -c "select count(*) from data_sources where workspace_id='$workspace_id' and code='akshare_sina_dce_fallback' and source_type='aggregator_public' and authorization_status='whitelisted_exception' and connector_code='akshare_v1'")" = 1
for dataset in futures_catalog_v1 trading_calendar_v1 daily_market_prices_v1 seat_positions_v1; do
  test "$(psql_value -c "select count(*) from import_batches batch join data_sources source on source.workspace_id=batch.workspace_id and source.id=batch.data_source_id where batch.workspace_id='$workspace_id' and batch.collection_date=date '$COLLECTION_DATE' and batch.dataset_type='$dataset' and batch.status='succeeded' and source.code='akshare_sina_dce_fallback'")" -ge 1
done

market_before=$(psql_value -c \
  "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
seats_before=$(psql_value -c \
  "select count(*) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
test "$market_before" -gt 0
test "$seats_before" -gt 0
test "$(psql_value -c "select count(*) from exchanges where workspace_id='$workspace_id'")" -ge 5
test "$(psql_value -c "select count(*) from contracts where workspace_id='$workspace_id'")" -gt 0
test "$(psql_value -c "select count(*) from market_prices price join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id join contracts contract on contract.workspace_id=price.workspace_id and contract.id=price.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where price.workspace_id='$workspace_id' and price.trade_date=date '$COLLECTION_DATE' and exchange.code='DCE' and source.code='akshare_sina_dce_fallback'")" -gt 0
test "$(psql_value -c "select count(*) from seat_positions position join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id join contracts contract on contract.workspace_id=position.workspace_id and contract.id=position.contract_id join instruments instrument on instrument.workspace_id=contract.workspace_id and instrument.id=contract.instrument_id join exchanges exchange on exchange.workspace_id=instrument.workspace_id and exchange.id=instrument.exchange_id where position.workspace_id='$workspace_id' and position.trade_date=date '$COLLECTION_DATE' and exchange.code='DCE' and source.code='akshare_sina_dce_fallback'")" -gt 0
test "$(psql_value -c "select count(*) from market_prices price join data_sources source on source.workspace_id=price.workspace_id and source.id=price.source_id where price.workspace_id='$workspace_id' and price.trade_date=date '$COLLECTION_DATE' and source.code='akshare_dce_official'")" = 0
test "$(psql_value -c "select count(*) from seat_positions position join data_sources source on source.workspace_id=position.workspace_id and source.id=position.source_id where position.workspace_id='$workspace_id' and position.trade_date=date '$COLLECTION_DATE' and source.code='akshare_dce_official'")" = 0

test "$(psql_value -c "select count(*) from (select workspace_id,source_id,contract_id,trade_date,session_type,granularity,revision_no,count(*) from market_prices where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0
test "$(psql_value -c "select count(*) from (select workspace_id,source_id,trade_date,contract_id,seat_id,rank_type,rank,count(*) from seat_positions where workspace_id='$workspace_id' group by 1,2,3,4,5,6,7 having count(*)>1) duplicate")" = 0

echo "PHASE4A_E2E_STAGE replay_run_started"
run_collector_with_peak "$EVIDENCE_DIR/replay-run.log"
echo "PHASE4A_E2E_STAGE replay_run_completed"
market_after=$(psql_value -c \
  "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
seats_after=$(psql_value -c \
  "select count(*) from seat_positions where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")
test "$market_after" = "$market_before"
test "$seats_after" = "$seats_before"

fault_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if run_collector_with_peak "$EVIDENCE_DIR/fault-run.log" \
  --dataset market --inject-failure-exchange DCE; then
  echo "PHASE4A_E2E_FAIL injected_failure_returned_success" >&2
  exit 1
fi
test "$(psql_value -c "select count(*) from extraction_jobs job join data_sources source on source.workspace_id=job.workspace_id and source.id=job.data_source_id where job.workspace_id='$workspace_id' and source.code='akshare_dce_official' and job.dataset_type='daily_market_prices_v1' and job.status='failed' and job.started_at >= timestamptz '$fault_started'")" -ge 1
test "$(psql_value -c "select count(distinct source.code) from extraction_jobs job join data_sources source on source.workspace_id=job.workspace_id and source.id=job.data_source_id where job.workspace_id='$workspace_id' and source.code<>'akshare_dce_official' and job.dataset_type='daily_market_prices_v1' and job.status='succeeded' and job.started_at >= timestamptz '$fault_started'")" = 4
test "$(psql_value -c "select count(*) from market_prices where workspace_id='$workspace_id' and trade_date=date '$COLLECTION_DATE'")" = "$market_before"

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
test "$peak_bytes" -gt 0
test "$peak_bytes" -le 536870912
test "$(psql_value -c "select count(*) from import_batches where ingestion_mode='manual'")" = "$legacy_batches_before"
test "$(psql_value -c "select md5(coalesce(string_agg(to_jsonb(batch)::text, '|' order by id), '')) from import_batches batch where ingestion_mode='manual'")" = "$legacy_batches_fingerprint_before"
test "$(psql_value -c "select count(*) from users")" = "$users_before"
test "$(psql_value -c "select md5(coalesce(string_agg(to_jsonb(app_user)::text, '|' order by id), '')) from users app_user")" = "$users_fingerprint_before"

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
  echo "dce_fallback_provenance=PASS"
} >"$EVIDENCE_DIR/result.env"
chmod 600 "$EVIDENCE_DIR/result.env"
echo PHASE4A_E2E_PASS
