#!/usr/bin/env bash
# Reproducible Phase 3C Docker/API/PostgreSQL acceptance harness.
# Run on the futures VPS after deploying this exact Git source state:
#   PHASE3C_ROOT=/opt/futures-platform \
#   PHASE3C_BASE_URL=http://127.0.0.1:8088 \
#   PHASE3C_ORIGIN=http://localhost:8088 \
#   bash rust/tests/phase_3c_e2e.sh
#
# Optional: PHASE3C_COMPOSE, PHASE3C_DB_USER, PHASE3C_DB_NAME,
# PHASE3C_COOKIE_NAME, PHASE3C_WORK_DIR. The harness generates isolated
# workspaces and credentials. It intentionally retains immutable import
# evidence, but its temporary retry trigger is always removed by trap.
set -euo pipefail

ROOT=${PHASE3C_ROOT:-/opt/futures-platform}
BASE=${PHASE3C_BASE_URL:-http://127.0.0.1:8088}
ORIGIN=${PHASE3C_ORIGIN:-http://localhost:8088}
COOKIE_NAME=${PHASE3C_COOKIE_NAME:-futures_session}
DB_USER=${PHASE3C_DB_USER:-futures_app}
DB_NAME=${PHASE3C_DB_NAME:-futures_platform}
WORK=${PHASE3C_WORK_DIR:-/tmp/phase3c-e2e-$$}
read -r -a COMPOSE_CMD <<<"${PHASE3C_COMPOSE:-docker compose}"

for command in base64 curl jq openssl python3 timeout grep seq sort tr; do
  command -v "$command" >/dev/null || {
    echo "PREREQ_FAIL missing=$command" >&2
    exit 1
  }
done
test -d "$ROOT" || {
  echo "PREREQ_FAIL missing_root=$ROOT" >&2
  exit 1
}
cd "$ROOT"
"${COMPOSE_CMD[@]}" config --quiet
mkdir -p "$WORK"

new_uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
WS1=$(new_uuid)
WS2=$(new_uuid)
USER1=$(new_uuid)
USER2=$(new_uuid)
SESSION1=$(new_uuid)
SESSION2=$(new_uuid)
MEMBERSHIP1=$(new_uuid)
MEMBERSHIP2=$(new_uuid)
USERNAME1="phase3c-e2e-${USER1:0:8}"
USERNAME2="phase3c-e2e-${USER2:0:8}"

psqlq() {
  "${COMPOSE_CMD[@]}" exec -T postgres psql -X -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 -Atq -c "$1"
}

hash_token() {
  printf '%s' "$1" | openssl dgst -sha256 -binary |
    base64 | tr '+/' '-_' | tr -d '=\n'
}

assert_eq() {
  if [ "$1" != "$2" ]; then
    echo "ASSERT_FAIL expected=$2 actual=$1 label=$3" >&2
    exit 1
  fi
}

assert_json() {
  local file=$1 expression=$2 label=$3
  if ! jq -e "$expression" "$file" >/dev/null; then
    echo "ASSERT_FAIL label=$label" >&2
    jq -c . "$file" >&2 || true
    exit 1
  fi
}

assert_psql_denied() {
  local sql=$1 label=$2
  if psqlq "$sql" >/dev/null 2>&1; then
    echo "ASSERT_FAIL label=$label expected=database_denial" >&2
    exit 1
  fi
}

assert_eq "$(psqlq "select count(*) from schema_versions where version in ('202607250008','202607250009')")" 2 "phase 3c migrations"
assert_eq "$(psqlq "select count(*) from information_schema.columns where table_schema='public' and table_name='job_queue' and column_name='lease_generation' and is_nullable='NO' and column_default='0'")" 1 "lease generation schema"
assert_eq "$(psqlq "select count(*) from pg_constraint where conname='job_queue_lease_generation_nonnegative'")" 1 "lease generation constraint"

TOKEN1=$(openssl rand -hex 32)
TOKEN2=$(openssl rand -hex 32)
CSRF1=$(openssl rand -hex 32)
CSRF2=$(openssl rand -hex 32)
TOKEN1_HASH=$(hash_token "$TOKEN1")
TOKEN2_HASH=$(hash_token "$TOKEN2")
CSRF1_HASH=$(hash_token "$CSRF1")
CSRF2_HASH=$(hash_token "$CSRF2")

drop_retry_trigger() {
  psqlq "drop trigger if exists phase3c_e2e_transient_failure on imported_records;
         drop trigger if exists phase3c_e2e_overlap_delay on imported_records;
         drop trigger if exists phase3c_e2e_parallel_delay on imported_records;
         drop trigger if exists phase3c_e2e_renewal_delay on imported_records;
         drop trigger if exists phase3c_e2e_exit_delay on imported_records;
         drop trigger if exists phase3c_e2e_always_failure on imported_records;
         drop function if exists app.phase3c_e2e_transient_failure();
         drop function if exists app.phase3c_e2e_overlap_delay();
         drop function if exists app.phase3c_e2e_parallel_delay();
         drop function if exists app.phase3c_e2e_renewal_delay();
         drop function if exists app.phase3c_e2e_exit_delay();
         drop function if exists app.phase3c_e2e_always_failure();" \
    >/dev/null 2>&1 || true
}

cleanup() {
  drop_retry_trigger
  "${COMPOSE_CMD[@]}" up -d --scale worker=1 worker >/dev/null 2>&1 || true
}
trap cleanup EXIT

stop_workers() {
  "${COMPOSE_CMD[@]}" stop worker >/dev/null
}

scale_workers() {
  "${COMPOSE_CMD[@]}" up -d --scale "worker=$1" worker >/dev/null
}

mutate() {
  local method=$1 path=$2 body=$3 output=$4
  curl -sS -o "$output" -w '%{http_code}' -X "$method" \
    -H "Cookie: $COOKIE_NAME=$TOKEN1" \
    -H "x-csrf-token: $CSRF1" \
    -H "Origin: $ORIGIN" \
    -H 'Content-Type: application/json' \
    --data "$body" "$BASE$path"
}

upload_file() {
  local file=$1 mime=$2 output=$3
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$TOKEN1" \
    -H "x-csrf-token: $CSRF1" \
    -H "Origin: $ORIGIN" \
    -F "file=@$file;type=$mime" "$BASE/api/v1/imports"
}

DEFAULT_MAPPING='{"dataset_type":"generic","template_version_id":null,"fields":[{"source_column":"date","target_field":"trade_date","transform":"trim"},{"source_column":"code","target_field":"code","transform":"trim"},{"source_column":"name","target_field":"name","transform":"trim"},{"source_column":"value","target_field":"value","transform":"trim"}]}'

prepare_batch() {
  local file=$1 mime=$2 label=$3 mapping=${4:-$DEFAULT_MAPPING}
  local out="$WORK/$label-upload.json"
  assert_eq "$(upload_file "$file" "$mime" "$out")" 201 "$label upload"
  local import_id
  import_id=$(jq -r '.data.id' "$out")
  assert_eq "$(mutate POST "/api/v1/imports/$import_id/inspect" '{}' "$WORK/$label-inspect.json")" 200 "$label inspect"
  assert_eq "$(mutate PUT "/api/v1/imports/$import_id/mapping" "$mapping" "$WORK/$label-mapping.json")" 200 "$label mapping"
  assert_eq "$(mutate POST "/api/v1/imports/$import_id/preview" '{}' "$WORK/$label-preview.json")" 200 "$label preview"
  assert_eq "$(mutate POST "/api/v1/imports/$import_id/validate" '{}' "$WORK/$label-validate.json")" 200 "$label validate"
  printf '%s' "$import_id"
}

confirm_batch() {
  local import_id=$1 policy=$2 key=$3 output=$4
  curl -sS -o "$output" -w '%{http_code}' -X POST \
    -H "Cookie: $COOKIE_NAME=$TOKEN1" \
    -H "x-csrf-token: $CSRF1" \
    -H "Origin: $ORIGIN" \
    -H "Idempotency-Key: $key" \
    -H 'Content-Type: application/json' \
    --data "{\"conflict_policy\":\"$policy\"}" \
    "$BASE/api/v1/imports/$import_id/confirm"
}

wait_batch() {
  local import_id=$1 expected=$2
  local body="$WORK/wait-$import_id.json"
  for _ in $(seq 1 320); do
    curl -fsS -H "Cookie: $COOKIE_NAME=$TOKEN1" \
      "$BASE/api/v1/imports/$import_id" >"$body"
    local status
    status=$(jq -r '.data.status' "$body")
    [ "$status" = "$expected" ] && return 0
    if [ "$status" = failed ]; then
      echo "ASSERT_FAIL import=$import_id terminal=$status expected=$expected" >&2
      exit 1
    fi
    sleep 0.25
  done
  echo "ASSERT_FAIL import=$import_id timeout expected=$expected" >&2
  exit 1
}

wait_job_sql() {
  local job_id=$1 expected=$2
  for _ in $(seq 1 320); do
    [ "$(psqlq "select status from job_queue where workspace_id='$WS1' and id='$job_id'")" = "$expected" ] && return 0
    sleep 0.25
  done
  echo "ASSERT_FAIL job=$job_id timeout expected=$expected" >&2
  exit 1
}

psqlq "
  insert into users(id,username,username_normalized,password_hash,password_params_version)
  values
    ('$USER1','$USERNAME1','$USERNAME1','unused-e2e-hash',1),
    ('$USER2','$USERNAME2','$USERNAME2','unused-e2e-hash',1);
  insert into workspaces(id,name,owner_user_id)
  values ('$WS1','Phase 3C E2E 1','$USER1'),('$WS2','Phase 3C E2E 2','$USER2');
  insert into workspace_memberships(id,workspace_id,user_id,role)
  values
    ('$MEMBERSHIP1','$WS1','$USER1','owner'),
    ('$MEMBERSHIP2','$WS2','$USER2','owner');
  insert into user_roles(user_id,role_name)
  values ('$USER1','analyst'),('$USER2','viewer');
  insert into sessions
    (id,user_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at,user_agent)
  values
    ('$SESSION1','$USER1','$TOKEN1_HASH','$CSRF1_HASH',now()+interval '2 hours',now()+interval '2 hours','phase3c-e2e'),
    ('$SESSION2','$USER2','$TOKEN2_HASH','$CSRF2_HASH',now()+interval '2 hours',now()+interval '2 hours','phase3c-e2e');
" >/dev/null

# More than the preview limit reaches validation and formal import. Twenty
# simultaneous confirmations converge to one job and one confirmation record.
printf 'date,code,name,value\n' >"$WORK/full.csv"
for i in $(seq -w 1 75); do
  printf '2026-07-25,C%s,Item%s,%s\n' "$i" "$i" "$((10#$i))" >>"$WORK/full.csv"
done
FULL_ID=$(prepare_batch "$WORK/full.csv" text/csv full)
assert_json "$WORK/full-validate.json" '.data.blocking_error_count == 0 and .data.duplicate_count == 0' "full validation"
IDEMPOTENCY=phase3c-e2e-concurrent-confirm-0001
PIDS=()
for i in $(seq 1 20); do
  (
    confirm_batch "$FULL_ID" skip "$IDEMPOTENCY" "$WORK/full-confirm-$i.json" \
      >"$WORK/full-confirm-$i.status"
  ) &
  PIDS+=("$!")
done
for pid in "${PIDS[@]}"; do wait "$pid"; done
FIRST_JOB=$(jq -r '.data.job_id' "$WORK/full-confirm-1.json")
REPLAYS=0
for i in $(seq 1 20); do
  assert_eq "$(cat "$WORK/full-confirm-$i.status")" 202 "concurrent confirm $i"
  assert_eq "$(jq -r '.data.job_id' "$WORK/full-confirm-$i.json")" "$FIRST_JOB" "single concurrent job $i"
  [ "$(jq -r '.data.replayed' "$WORK/full-confirm-$i.json")" = true ] && REPLAYS=$((REPLAYS + 1))
done
assert_eq "$REPLAYS" 19 "nineteen idempotent replays"
assert_eq "$(psqlq "select count(*) from import_confirmations where workspace_id='$WS1' and import_batch_id='$FULL_ID'")" 1 "one confirmation row"
wait_batch "$FULL_ID" succeeded
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$FULL_ID'")" 75 "all rows imported"

# Workspace-wide idempotency matrix:
# same key/same parameters above; same key/different parameters; different
# key/same parameters; different key/different parameters.
assert_eq "$(confirm_batch "$FULL_ID" overwrite "$IDEMPOTENCY" "$WORK/idem-same-key-different.json")" 409 "same key different parameters"
assert_json "$WORK/idem-same-key-different.json" '.data.code == "idempotency_key_reused"' "same key different parameters body"
assert_eq "$(confirm_batch "$FULL_ID" skip phase3c-e2e-different-key-same-0001 "$WORK/idem-different-key-same.json")" 202 "different key same parameters"
assert_json "$WORK/idem-different-key-same.json" ".data.job_id == \"$FIRST_JOB\" and .data.replayed == true" "different key same parameters replay"
assert_eq "$(confirm_batch "$FULL_ID" overwrite phase3c-e2e-different-key-different-1 "$WORK/idem-different-key-different.json")" 409 "different key different parameters"
assert_json "$WORK/idem-different-key-different.json" '.data.code == "confirmation_conflict"' "different key different parameters body"
assert_eq "$(psqlq "select count(*) from import_confirmations where workspace_id='$WS1' and import_batch_id='$FULL_ID'")" 2 "two accepted idempotency identities"

# A committed batch cannot be enqueued or claimed again. Two workers poll the
# queue after success; formal rows and terminal events remain exactly once.
assert_psql_denied "
  begin;
  select set_config('app.current_workspace_id','$WS1',true);
  insert into job_queue
    (id,workspace_id,job_type,aggregate_id,status,payload)
  values ('$(new_uuid)','$WS1','import_confirm','$FULL_ID','queued',
          jsonb_build_object('import_id','$FULL_ID'));
  commit;
" "duplicate import_confirm queue identity"
scale_workers 2
sleep 2
scale_workers 1
assert_eq "$(psqlq "select status || ':' || attempt_count::text || ':' || lease_generation::text from job_queue where id='$FIRST_JOB'")" "succeeded:1:1" "successful job is not reclaimed"
assert_eq "$(psqlq "select count(*) from job_queue where workspace_id='$WS1' and aggregate_id='$FULL_ID' and ((status='queued' and available_at<=now()) or (status='running' and lease_expires_at<now()))")" 0 "successful job is ineligible"
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$FULL_ID'")" 75 "repeat polling creates no writes"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$FIRST_JOB' and event_type='succeeded'")" 1 "repeat polling creates no terminal event"

# The same workspace key racing across different batches is serialized before
# either batch lock. Exactly one request is accepted and the loser receives the
# stable domain 409 rather than a unique-violation 500.
printf 'date,code,name,value\n2026-08-02,IDEMA,Cross batch A,1\n' >"$WORK/idem-cross-a.csv"
printf 'date,code,name,value\n2026-08-03,IDEMB,Cross batch B,2\n' >"$WORK/idem-cross-b.csv"
IDEM_CROSS_A=$(prepare_batch "$WORK/idem-cross-a.csv" text/csv idem-cross-a)
IDEM_CROSS_B=$(prepare_batch "$WORK/idem-cross-b.csv" text/csv idem-cross-b)
CROSS_KEY=phase3c-e2e-cross-batch-shared-key-01
(
  confirm_batch "$IDEM_CROSS_A" skip "$CROSS_KEY" "$WORK/idem-cross-a-confirm.json" \
    >"$WORK/idem-cross-a-confirm.status"
) &
CROSS_PID_A=$!
(
  confirm_batch "$IDEM_CROSS_B" overwrite "$CROSS_KEY" "$WORK/idem-cross-b-confirm.json" \
    >"$WORK/idem-cross-b-confirm.status"
) &
CROSS_PID_B=$!
wait "$CROSS_PID_A" "$CROSS_PID_B"
CROSS_CODES=$(printf '%s\n%s\n' \
  "$(cat "$WORK/idem-cross-a-confirm.status")" \
  "$(cat "$WORK/idem-cross-b-confirm.status")" | sort | tr '\n' ':')
assert_eq "$CROSS_CODES" "202:409:" "cross batch same key serialized"
if [ "$(cat "$WORK/idem-cross-a-confirm.status")" = 409 ]; then
  assert_json "$WORK/idem-cross-a-confirm.json" '.data.code == "idempotency_key_reused"' "cross batch A stable conflict"
  IDEM_CROSS_ACCEPTED=$IDEM_CROSS_B
else
  assert_json "$WORK/idem-cross-b-confirm.json" '.data.code == "idempotency_key_reused"' "cross batch B stable conflict"
  IDEM_CROSS_ACCEPTED=$IDEM_CROSS_A
fi
assert_eq "$(psqlq "select count(*) from import_confirmations where workspace_id='$WS1' and idempotency_key_hash = (select idempotency_key_hash from import_confirmations where import_batch_id in ('$IDEM_CROSS_A','$IDEM_CROSS_B') limit 1) and import_batch_id in ('$IDEM_CROSS_A','$IDEM_CROSS_B')")" 1 "cross batch one confirmation"
wait_batch "$IDEM_CROSS_ACCEPTED" succeeded

timeout 15 curl -fsSN -H "Cookie: $COOKIE_NAME=$TOKEN1" \
  "$BASE/api/v1/imports/$FULL_ID/events" >"$WORK/events-all.txt"
grep -q '^event: queued' "$WORK/events-all.txt"
grep -q '^event: succeeded' "$WORK/events-all.txt"
timeout 15 curl -fsSN -H "Cookie: $COOKIE_NAME=$TOKEN1" -H 'Last-Event-ID: 1' \
  "$BASE/api/v1/imports/$FULL_ID/events" >"$WORK/events-replay.txt"
! grep -q '^id: 1$' "$WORK/events-replay.txt"
grep -q '^event: succeeded' "$WORK/events-replay.txt"
INVALID_EVENT_STATUS=$(curl -sS -o "$WORK/events-invalid.json" -w '%{http_code}' \
  -H "Cookie: $COOKIE_NAME=$TOKEN1" -H 'Last-Event-ID: not-an-integer' \
  "$BASE/api/v1/imports/$FULL_ID/events")
assert_eq "$INVALID_EVENT_STATUS" 400 "invalid Last-Event-ID"
assert_json "$WORK/events-invalid.json" '.data.code == "event_id_invalid"' "invalid Last-Event-ID body"

# Deterministic transforms have identical preview/full-validation values.
printf 'date,code,name,value\n20260725,T1,  Trim Me  ,"1,234.50"\n' >"$WORK/transforms.csv"
TRANSFORM_MAPPING='{"dataset_type":"generic","template_version_id":null,"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"},{"source_column":"code","target_field":"code","transform":"trim"},{"source_column":"name","target_field":"name","transform":"trim"},{"source_column":"value","target_field":"value","transform":"decimal"}]}'
TRANSFORM_ID=$(prepare_batch "$WORK/transforms.csv" text/csv transforms "$TRANSFORM_MAPPING")
assert_json "$WORK/transforms-preview.json" '.data.preview_rows[0].cells[0].normalized_value == "2026-07-25" and .data.preview_rows[0].cells[2].normalized_value == "Trim Me" and .data.preview_rows[0].cells[3].normalized_value == "1234.50"' "transform preview"
assert_eq "$(confirm_batch "$TRANSFORM_ID" skip phase3c-e2e-transform-0001 "$WORK/transforms-confirm.json")" 202 "transform confirm"
wait_batch "$TRANSFORM_ID" succeeded
assert_eq "$(psqlq "select (record_data->>'trade_date') || ':' || (record_data->>'name') || ':' || (record_data->>'value') from imported_records where workspace_id='$WS1' and source_import_batch_id='$TRANSFORM_ID'")" "2026-07-25:Trim Me:1234.50" "formal transformed record"

# Database conflicts: overwrite, skip, keep_conflict; file duplicates and abort.
printf 'date,code,name,value\n2026-07-25,C01,Overwrite,999\n' >"$WORK/overwrite.csv"
OVERWRITE_ID=$(prepare_batch "$WORK/overwrite.csv" text/csv overwrite)
assert_json "$WORK/overwrite-validate.json" '.data.conflict_count == 1' "overwrite detects conflict"
assert_eq "$(confirm_batch "$OVERWRITE_ID" overwrite phase3c-e2e-overwrite-0001 "$WORK/overwrite-confirm.json")" 202 "overwrite confirm"
wait_batch "$OVERWRITE_ID" succeeded
assert_eq "$(psqlq "select record_data->>'value' from imported_records where workspace_id='$WS1' and business_key='2026-07-25|C01'")" 999 "overwrite value"
assert_eq "$(psqlq "select row_version from imported_records where workspace_id='$WS1' and business_key='2026-07-25|C01'")" 2 "overwrite version"

printf 'date,code,name,value\n2026-07-25,C01,Skip,888\n' >"$WORK/skip.csv"
SKIP_ID=$(prepare_batch "$WORK/skip.csv" text/csv skip)
assert_eq "$(confirm_batch "$SKIP_ID" skip phase3c-e2e-skip-00000001 "$WORK/skip-confirm.json")" 202 "skip confirm"
wait_batch "$SKIP_ID" succeeded
assert_eq "$(psqlq "select record_data->>'value' from imported_records where workspace_id='$WS1' and business_key='2026-07-25|C01'")" 999 "skip preserves value"
assert_json "$WORK/wait-$SKIP_ID.json" '.data.job.skipped_count == 1' "skip count"

printf 'date,code,name,value\n2026-07-25,C01,Conflict,777\n' >"$WORK/conflict.csv"
CONFLICT_ID=$(prepare_batch "$WORK/conflict.csv" text/csv conflict)
assert_eq "$(confirm_batch "$CONFLICT_ID" keep_conflict phase3c-e2e-conflict-0001 "$WORK/conflict-confirm.json")" 202 "keep conflict confirm"
wait_batch "$CONFLICT_ID" succeeded
assert_eq "$(psqlq "select count(*) from import_conflict_candidates where workspace_id='$WS1' and import_batch_id='$CONFLICT_ID' and conflict_kind='database_conflict'")" 1 "database conflict candidate"

printf 'date,code,name,value\n2026-07-26,DUP1,Duplicate A,1\n2026-07-26,DUP1,Duplicate B,2\n' >"$WORK/duplicate.csv"
DUP_ID=$(prepare_batch "$WORK/duplicate.csv" text/csv duplicate)
assert_json "$WORK/duplicate-validate.json" '.data.duplicate_count == 2' "file duplicate detection"
assert_eq "$(confirm_batch "$DUP_ID" keep_conflict phase3c-e2e-duplicate-0001 "$WORK/duplicate-confirm.json")" 202 "duplicate keep conflict"
wait_batch "$DUP_ID" succeeded
assert_eq "$(psqlq "select count(*) from import_conflict_candidates where workspace_id='$WS1' and import_batch_id='$DUP_ID' and conflict_kind='file_duplicate'")" 2 "file duplicate candidates"
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and business_key='2026-07-26|DUP1'")" 0 "duplicate not imported"

printf 'date,code,name,value\n2026-07-25,C01,Abort,666\n' >"$WORK/abort.csv"
ABORT_ID=$(prepare_batch "$WORK/abort.csv" text/csv abort)
assert_eq "$(confirm_batch "$ABORT_ID" abort phase3c-e2e-abort-00000001 "$WORK/abort-confirm.json")" 400 "abort conflict"
assert_json "$WORK/abort-confirm.json" '.data.code == "blocking_errors_present"' "abort error"
assert_eq "$(psqlq "select count(*) from job_queue where workspace_id='$WS1' and aggregate_id='$ABORT_ID'")" 0 "abort no job"

# Complete conflict matrix required by Phase 3C:
#   4 policies x {file duplicate, existing DB conflict,
#                 conflict inserted after validation and before Worker}.
MATRIX_POLICIES=(skip overwrite keep_conflict abort)

# Matrix A: duplicate business keys inside the same file.
for policy in "${MATRIX_POLICIES[@]}"; do
  code="MFD_${policy^^}"
  file="$WORK/matrix-file-$policy.csv"
  label="matrix-file-$policy"
  printf 'date,code,name,value\n2026-09-10,%s,First,11\n2026-09-10,%s,Last,22\n' \
    "$code" "$code" >"$file"
  matrix_id=$(prepare_batch "$file" text/csv "$label")
  assert_json "$WORK/$label-validate.json" '.data.duplicate_count == 2 and .data.conflict_count == 0' "$label validation"
  status=$(confirm_batch "$matrix_id" "$policy" "phase3c-matrix-file-$policy-key-01" "$WORK/$label-confirm.json")
  business_key="2026-09-10|$code"
  if [ "$policy" = abort ]; then
    assert_eq "$status" 400 "$label abort confirmation"
    assert_json "$WORK/$label-confirm.json" '.data.code == "blocking_errors_present"' "$label abort body"
    assert_eq "$(psqlq "select count(*) from job_queue where aggregate_id='$matrix_id'")" 0 "$label abort no job"
    assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and business_key='$business_key'")" 0 "$label abort no writes"
    assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label abort no partial candidates"
  else
    assert_eq "$status" 202 "$label confirmation"
    wait_batch "$matrix_id" succeeded
    case "$policy" in
      skip)
        assert_eq "$(psqlq "select record_data->>'value' from imported_records where workspace_id='$WS1' and business_key='$business_key'")" 11 "$label keeps first"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 1 "$label one formal row"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidates"
        ;;
      overwrite)
        assert_eq "$(psqlq "select record_data->>'value' from imported_records where workspace_id='$WS1' and business_key='$business_key'")" 22 "$label keeps last"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 1 "$label one formal row"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidates"
        ;;
      keep_conflict)
        assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and business_key='$business_key'")" 0 "$label imports none"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id' and conflict_kind='file_duplicate'")" 2 "$label keeps both candidates"
        ;;
    esac
  fi
done

# Seed one existing record per policy for Matrix B.
printf 'date,code,name,value\n' >"$WORK/matrix-db-seed.csv"
for policy in "${MATRIX_POLICIES[@]}"; do
  printf '2026-09-11,MDB_%s,Seed,100\n' "${policy^^}" >>"$WORK/matrix-db-seed.csv"
done
MATRIX_SEED_ID=$(prepare_batch "$WORK/matrix-db-seed.csv" text/csv matrix-db-seed)
assert_eq "$(confirm_batch "$MATRIX_SEED_ID" skip phase3c-matrix-db-seed-key-0001 "$WORK/matrix-db-seed-confirm.json")" 202 "matrix DB seed confirm"
wait_batch "$MATRIX_SEED_ID" succeeded
assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$MATRIX_SEED_ID'")" 4 "matrix DB seed rows"

# Matrix B: conflict already exists when validation runs.
for policy in "${MATRIX_POLICIES[@]}"; do
  code="MDB_${policy^^}"
  file="$WORK/matrix-db-$policy.csv"
  label="matrix-db-$policy"
  printf 'date,code,name,value\n2026-09-11,%s,Candidate,200\n' "$code" >"$file"
  matrix_id=$(prepare_batch "$file" text/csv "$label")
  assert_json "$WORK/$label-validate.json" '.data.duplicate_count == 0 and .data.conflict_count == 1' "$label validation"
  status=$(confirm_batch "$matrix_id" "$policy" "phase3c-matrix-db-$policy-key-00001" "$WORK/$label-confirm.json")
  business_key="2026-09-11|$code"
  if [ "$policy" = abort ]; then
    assert_eq "$status" 400 "$label abort confirmation"
    assert_json "$WORK/$label-confirm.json" '.data.code == "blocking_errors_present"' "$label abort body"
    assert_eq "$(psqlq "select count(*) from job_queue where aggregate_id='$matrix_id'")" 0 "$label abort no job"
    assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "100:1" "$label abort preserves existing"
    assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label abort no partial records"
    assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label abort no partial candidates"
  else
    assert_eq "$status" 202 "$label confirmation"
    wait_batch "$matrix_id" succeeded
    case "$policy" in
      skip)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "100:1" "$label preserves existing"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label candidate skipped"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidate"
        ;;
      overwrite)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "200:2" "$label overwrites existing"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 1 "$label owns overwritten row"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidate"
        ;;
      keep_conflict)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "100:1" "$label preserves existing"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label imports no candidate"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id' and conflict_kind='database_conflict'")" 1 "$label candidate"
        ;;
    esac
  fi
done

# Matrix C: validation reports no conflict, then a competing record is inserted
# while Workers are stopped. All four jobs execute against the new conflict.
declare -A MATRIX_CONCURRENT_IDS
for policy in "${MATRIX_POLICIES[@]}"; do
  code="MCON_${policy^^}"
  file="$WORK/matrix-concurrent-$policy.csv"
  label="matrix-concurrent-$policy"
  printf 'date,code,name,value\n2026-09-12,%s,Candidate,400\n' "$code" >"$file"
  MATRIX_CONCURRENT_IDS[$policy]=$(prepare_batch "$file" text/csv "$label")
  assert_json "$WORK/$label-validate.json" '.data.duplicate_count == 0 and .data.conflict_count == 0' "$label validation before race"
done
stop_workers
matrix_row=200
for policy in "${MATRIX_POLICIES[@]}"; do
  matrix_id=${MATRIX_CONCURRENT_IDS[$policy]}
  label="matrix-concurrent-$policy"
  assert_eq "$(confirm_batch "$matrix_id" "$policy" "phase3c-matrix-concurrent-$policy-key-1" "$WORK/$label-confirm.json")" 202 "$label confirmation before race"
  code="MCON_${policy^^}"
  psqlq "
    begin;
    select set_config('app.current_workspace_id','$WS1',true);
    insert into imported_records
      (id,workspace_id,dataset_type,business_key,record_data,
       source_import_batch_id,source_row_number,row_version,created_by)
    values
      ('$(new_uuid)','$WS1','generic','2026-09-12|$code',
       jsonb_build_object('trade_date','2026-09-12','code','$code','name','Competing','value','300'),
       '$MATRIX_SEED_ID',$matrix_row,1,'$USER1')
    ;
    commit;
  " >/dev/null
  matrix_row=$((matrix_row + 1))
done
scale_workers 1
for policy in "${MATRIX_POLICIES[@]}"; do
  matrix_id=${MATRIX_CONCURRENT_IDS[$policy]}
  label="matrix-concurrent-$policy"
  business_key="2026-09-12|MCON_${policy^^}"
  if [ "$policy" = abort ]; then
    wait_batch "$matrix_id" failed
    assert_eq "$(psqlq "select status || ':' || coalesce(last_error_code,'') from job_queue where aggregate_id='$matrix_id'")" "failed:abort_conflict" "$label worker abort"
    assert_eq "$(psqlq "select attempt_count || ':' || lease_generation from job_queue where aggregate_id='$matrix_id'")" "1:1" "$label permanent failure is not retried"
    assert_eq "$(psqlq "select count(*) from import_job_events where import_batch_id='$matrix_id' and event_type='running'")" 1 "$label one execution attempt"
    assert_eq "$(psqlq "select count(*) from import_job_events where import_batch_id='$matrix_id' and event_type='progress' and payload->>'status'='queued'")" 0 "$label no retry event"
    assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "300:1" "$label preserves competitor"
    assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label no partial formal writes"
    assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no partial conflict candidates"
  else
    wait_batch "$matrix_id" succeeded
    case "$policy" in
      skip)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "300:1" "$label preserves competitor"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label candidate skipped"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidate"
        ;;
      overwrite)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "400:2" "$label overwrites competitor"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 1 "$label owns overwritten row"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id'")" 0 "$label no candidate"
        ;;
      keep_conflict)
        assert_eq "$(psqlq "select (record_data->>'value') || ':' || row_version::text from imported_records where workspace_id='$WS1' and business_key='$business_key'")" "300:1" "$label preserves competitor"
        assert_eq "$(psqlq "select count(*) from imported_records where source_import_batch_id='$matrix_id'")" 0 "$label imports no candidate"
        assert_eq "$(psqlq "select count(*) from import_conflict_candidates where import_batch_id='$matrix_id' and conflict_kind='database_conflict'")" 1 "$label candidate"
        ;;
    esac
  fi
done

# Validation errors, stable cursor paging, and formula warnings rebuilt by full
# XLSX parsing (the warning contains no cell value).
printf 'date,code,name,value\n2026-07-27,,No code,abc\n2026-99-99,BAD2,,2\n2026-07-28,,,3\n' >"$WORK/bad.csv"
BAD_ID=$(prepare_batch "$WORK/bad.csv" text/csv bad)
assert_json "$WORK/bad-validate.json" '.data.blocking_error_count >= 3 and .data.warning_count >= 1' "validation rules"
curl -fsS -H "Cookie: $COOKIE_NAME=$TOKEN1" \
  "$BASE/api/v1/imports/$BAD_ID/errors?limit=1" >"$WORK/errors-page1.json"
CURSOR=$(jq -r '.data.next_cursor' "$WORK/errors-page1.json")
test -n "$CURSOR" && test "$CURSOR" != null
curl -fsS -G -H "Cookie: $COOKIE_NAME=$TOKEN1" \
  --data-urlencode "cursor=$CURSOR" --data-urlencode 'limit=1' \
  "$BASE/api/v1/imports/$BAD_ID/errors" >"$WORK/errors-page2.json"
assert_json "$WORK/errors-page1.json" '.data.items | length == 1' "errors page one"
assert_json "$WORK/errors-page2.json" '.data.items | length == 1' "errors page two"

python3 - "$WORK/formula.xlsx" <<'PY'
import sys, zipfile
out = sys.argv[1]
parts = {
"[Content_Types].xml": """<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>""",
"_rels/.rels": """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""",
"xl/workbook.xml": """<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>""",
"xl/_rels/workbook.xml.rels": """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>""",
"xl/worksheets/sheet1.xml": """<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>date</t></is></c><c r="B1" t="inlineStr"><is><t>code</t></is></c><c r="C1" t="inlineStr"><is><t>name</t></is></c><c r="D1" t="inlineStr"><is><t>value</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>2026-08-01</t></is></c><c r="B2" t="inlineStr"><is><t>FORM1</t></is></c><c r="C2" t="inlineStr"><is><t>Formula</t></is></c><c r="D2"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>"""
}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in parts.items():
        z.writestr(name, data)
PY
FORMULA_ID=$(prepare_batch "$WORK/formula.xlsx" application/vnd.openxmlformats-officedocument.spreadsheetml.sheet formula)
assert_json "$WORK/formula-validate.json" '.data.warning_count >= 1' "formula warning survives full validation"
curl -fsS -H "Cookie: $COOKIE_NAME=$TOKEN1" \
  "$BASE/api/v1/imports/$FORMULA_ID/errors?limit=200" >"$WORK/formula-errors.json"
assert_json "$WORK/formula-errors.json" '[.data.items[] | select(.error_code == "formula_detected" and .raw_value == null)] | length >= 1' "formula warning is rebuilt and redacted"
assert_json "$WORK/formula-errors.json" '[.data.items[] | select(.error_code == "formula_detected" and .message == "检测到电子表格公式；仅导入缓存值，不执行公式。")] | length >= 1' "formula warning uses localized message"

# Cross-workspace API/SSE, direct RLS, and common write rejection audits.
WS2_GET=$(curl -sS -o "$WORK/ws2-get.json" -w '%{http_code}' \
  -H "Cookie: $COOKIE_NAME=$TOKEN2" "$BASE/api/v1/imports/$FULL_ID")
WS2_SSE=$(curl -sS -o "$WORK/ws2-sse.json" -w '%{http_code}' \
  -H "Cookie: $COOKIE_NAME=$TOKEN2" "$BASE/api/v1/imports/$FULL_ID/events")
assert_eq "$WS2_GET" 404 "cross workspace GET"
assert_eq "$WS2_SSE" 404 "cross workspace SSE"
RLS_COUNTS=$(psqlq "
  begin;
  set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  select
    (select count(*) from imported_records where workspace_id='$WS1')::text || ':' ||
    (select count(*) from import_conflict_candidates where workspace_id='$WS1')::text || ':' ||
    (select count(*) from job_queue where workspace_id='$WS1')::text || ':' ||
    (select count(*) from import_confirmations where workspace_id='$WS1')::text || ':' ||
    (select count(*) from import_job_events where workspace_id='$WS1')::text;
  rollback;
" | tail -n 1)
assert_eq "$RLS_COUNTS" "0:0:0:0:0" "all Phase 3C tables hide other workspace"
assert_eq "$(psqlq "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname in ('imported_records','import_conflict_candidates','job_queue','import_confirmations','import_job_events') and c.relrowsecurity and c.relforcerowsecurity")" 5 "all Phase 3C tables force RLS"
RLS_UPDATE_COUNTS=$(psqlq "
  begin;
  set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  with changed as (
    update imported_records set record_data=record_data
    where workspace_id='$WS1' returning 1
  ) select count(*) from changed;
  with changed as (
    update job_queue set updated_at=updated_at
    where workspace_id='$WS1' returning 1
  ) select count(*) from changed;
  rollback;
" | tail -n 2 | tr '\n' ':')
assert_eq "$RLS_UPDATE_COUNTS" "0:0:" "runtime cross-workspace updates rejected"

RLS_STAGING_ID=$(psqlq "select id from import_staging_rows where workspace_id='$WS1' and import_batch_id='$FULL_ID' order by row_number limit 1")
assert_psql_denied "
  begin; set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  insert into imported_records
    (id,workspace_id,dataset_type,business_key,record_data,
     source_import_batch_id,source_row_number,row_version,created_by)
  values ('$(new_uuid)','$WS1','generic','RLS|IMPORTED','{}',
          '$FULL_ID',999,1,'$USER1');
  commit;
" "RLS rejects imported_records cross-workspace insert"
assert_psql_denied "
  begin; set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  insert into import_conflict_candidates
    (id,workspace_id,import_batch_id,staging_row_id,dataset_type,
     business_key,candidate_data,conflict_kind)
  values ('$(new_uuid)','$WS1','$FULL_ID','$RLS_STAGING_ID','generic',
          'RLS|CONFLICT','{}','file_duplicate');
  commit;
" "RLS rejects import_conflict_candidates cross-workspace insert"
assert_psql_denied "
  begin; set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  insert into job_queue
    (id,workspace_id,job_type,aggregate_id,status,payload)
  values ('$(new_uuid)','$WS1','rls_probe','$FULL_ID','queued','{}');
  commit;
" "RLS rejects job_queue cross-workspace insert"
assert_psql_denied "
  begin; set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  insert into import_confirmations
    (id,workspace_id,import_batch_id,idempotency_key_hash,request_hash,
     job_id,confirmed_by)
  values ('$(new_uuid)','$WS1','$FULL_ID',
          repeat('a',64),repeat('b',64),'$FIRST_JOB','$USER1');
  commit;
" "RLS rejects import_confirmations cross-workspace insert"
assert_psql_denied "
  begin; set local role futures_runtime;
  select set_config('app.current_workspace_id','$WS2',true);
  insert into import_job_events
    (id,workspace_id,import_batch_id,job_id,event_seq,event_type,payload)
  values ('$(new_uuid)','$WS1','$FULL_ID','$FIRST_JOB',999999,'progress','{}');
  commit;
" "RLS rejects import_job_events cross-workspace insert"

ORIGIN_DENIED=$(curl -sS -o "$WORK/origin-denied.json" -w '%{http_code}' -X POST \
  -H "Cookie: $COOKIE_NAME=$TOKEN1" -H "x-csrf-token: $CSRF1" \
  -H 'Origin: https://invalid.example' -H 'Content-Type: application/json' \
  --data '{}' "$BASE/api/v1/imports/$FULL_ID/inspect")
CSRF_DENIED=$(curl -sS -o "$WORK/csrf-denied.json" -w '%{http_code}' -X POST \
  -H "Cookie: $COOKIE_NAME=$TOKEN1" -H 'x-csrf-token: wrong-token' \
  -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  --data '{}' "$BASE/api/v1/imports/$FULL_ID/inspect")
PERMISSION_DENIED=$(curl -sS -o "$WORK/permission-denied.json" -w '%{http_code}' -X POST \
  -H "Cookie: $COOKIE_NAME=$TOKEN2" -H "x-csrf-token: $CSRF2" \
  -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  --data '{}' "$BASE/api/v1/imports/$FULL_ID/inspect")
assert_eq "$ORIGIN_DENIED" 403 "origin denied"
assert_eq "$CSRF_DENIED" 403 "csrf denied"
assert_eq "$PERMISSION_DENIED" 403 "permission denied"
assert_eq "$(psqlq "select count(*) from audit_logs where event_type='import.write' and outcome='denied' and metadata->>'reason_code' in ('origin_mismatch','csrf_invalid','permission_denied') and workspace_id in ('$WS1','$WS2')")" 3 "write denials audited"
assert_eq "$(psqlq "select count(*) from audit_logs where workspace_id='$WS1' and event_type='import.confirm' and outcome='failure' and metadata->>'import_id'='$ABORT_ID' and metadata->>'reason_code'='blocking_errors_present'")" 1 "confirm rejection audited"

# A long-running task must renew the same generation before its original lease
# expires. The trigger is scoped to one isolated business key.
printf 'date,code,name,value\n2026-08-07,RENEW1,Lease renewal,1\n' >"$WORK/renewal.csv"
RENEWAL_ID=$(prepare_batch "$WORK/renewal.csv" text/csv renewal)
stop_workers
assert_eq "$(confirm_batch "$RENEWAL_ID" skip phase3c-e2e-renewal-key-00001 "$WORK/renewal-confirm.json")" 202 "renewal confirm"
RENEWAL_JOB=$(jq -r '.data.job_id' "$WORK/renewal-confirm.json")
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_renewal_delay()
  returns trigger language plpgsql as \$\$
  begin
    if new.workspace_id = '$WS1'::uuid
       and new.business_key = '2026-08-07|RENEW1' then
      perform pg_sleep(14);
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_renewal_delay
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_renewal_delay();
" >/dev/null
scale_workers 1
RENEW_BEFORE=
for _ in $(seq 1 100); do
  RENEW_STATE=$(psqlq "select status || ':' || lease_generation::text || ':' || extract(epoch from lease_expires_at)::bigint::text from job_queue where id='$RENEWAL_JOB'")
  if [[ "$RENEW_STATE" == running:1:* ]]; then
    RENEW_BEFORE=${RENEW_STATE#running:1:}
    break
  fi
  sleep 0.1
done
test -n "$RENEW_BEFORE" || {
  echo "ASSERT_FAIL renewal task did not start" >&2
  exit 1
}
sleep 11
RENEW_AFTER_STATE=$(psqlq "select status || ':' || lease_generation::text || ':' || extract(epoch from lease_expires_at)::bigint::text from job_queue where id='$RENEWAL_JOB'")
if [[ "$RENEW_AFTER_STATE" != running:1:* ]]; then
  echo "ASSERT_FAIL renewal task was not still running state=$RENEW_AFTER_STATE" >&2
  exit 1
fi
RENEW_AFTER=${RENEW_AFTER_STATE#running:1:}
if [ "$RENEW_AFTER" -le "$RENEW_BEFORE" ]; then
  echo "ASSERT_FAIL lease expiry did not advance before=$RENEW_BEFORE after=$RENEW_AFTER" >&2
  exit 1
fi
wait_batch "$RENEWAL_ID" succeeded
assert_eq "$(psqlq "select attempt_count || ':' || lease_generation from job_queue where id='$RENEWAL_JOB'")" "1:1" "renewal keeps same generation"
drop_retry_trigger

# Stop the actual Worker while its formal transaction is sleeping. No lease
# row is edited: recovery starts only after the genuine lease expires.
printf 'date,code,name,value\n2026-08-08,EXIT1,Worker exit recovery,1\n' >"$WORK/exit-recovery.csv"
EXIT_ID=$(prepare_batch "$WORK/exit-recovery.csv" text/csv exit-recovery)
stop_workers
assert_eq "$(confirm_batch "$EXIT_ID" skip phase3c-e2e-exit-key-0000001 "$WORK/exit-confirm.json")" 202 "exit recovery confirm"
EXIT_JOB=$(jq -r '.data.job_id' "$WORK/exit-confirm.json")
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_exit_delay()
  returns trigger language plpgsql as \$\$
  begin
    if new.workspace_id = '$WS1'::uuid
       and new.business_key = '2026-08-08|EXIT1' then
      perform pg_sleep(45);
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_exit_delay
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_exit_delay();
" >/dev/null
scale_workers 1
for _ in $(seq 1 100); do
  EXIT_STATE=$(psqlq "select status || ':' || lease_generation::text from job_queue where id='$EXIT_JOB'")
  [ "$EXIT_STATE" = "running:1" ] && break
  sleep 0.1
done
assert_eq "${EXIT_STATE:-missing}" "running:1" "worker processing before exit"
sleep 1
"${COMPOSE_CMD[@]}" kill -s KILL worker >/dev/null
assert_eq "$(psqlq "select status || ':' || lease_generation::text from job_queue where id='$EXIT_JOB'")" "running:1" "worker exit leaves leased job"
EXIT_EXPIRED=0
for _ in $(seq 1 200); do
  if [ "$(psqlq "select (lease_expires_at < now())::text from job_queue where id='$EXIT_JOB'")" = true ]; then
    EXIT_EXPIRED=1
    break
  fi
  sleep 0.25
done
assert_eq "$EXIT_EXPIRED" 1 "exited worker lease expires naturally"
drop_retry_trigger
scale_workers 1
wait_batch "$EXIT_ID" succeeded
assert_eq "$(psqlq "select attempt_count || ':' || lease_generation from job_queue where id='$EXIT_JOB'")" "2:2" "worker restart reclaims expired lease"

# Two workers must claim different unlocked jobs concurrently. The delay keeps
# both claims observable while lease renewal remains active.
printf 'date,code,name,value\n2026-08-04,PAR1,Parallel one,1\n' >"$WORK/parallel-1.csv"
printf 'date,code,name,value\n2026-08-05,PAR2,Parallel two,2\n' >"$WORK/parallel-2.csv"
PARALLEL_ID_1=$(prepare_batch "$WORK/parallel-1.csv" text/csv parallel-1)
PARALLEL_ID_2=$(prepare_batch "$WORK/parallel-2.csv" text/csv parallel-2)
stop_workers
assert_eq "$(confirm_batch "$PARALLEL_ID_1" skip phase3c-e2e-parallel-key-0001 "$WORK/parallel-1-confirm.json")" 202 "parallel one confirm"
assert_eq "$(confirm_batch "$PARALLEL_ID_2" skip phase3c-e2e-parallel-key-0002 "$WORK/parallel-2-confirm.json")" 202 "parallel two confirm"
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_parallel_delay()
  returns trigger language plpgsql as \$\$
  begin
    if new.workspace_id = '$WS1'::uuid
       and new.business_key in ('2026-08-04|PAR1','2026-08-05|PAR2') then
      perform pg_sleep(6);
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_parallel_delay
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_parallel_delay();
" >/dev/null
scale_workers 2
PARALLEL_OBSERVED=0
for _ in $(seq 1 80); do
  PARALLEL_STATE=$(psqlq "
    select count(*)::text || ':' || count(distinct leased_by)::text
    from job_queue
    where workspace_id='$WS1'
      and aggregate_id in ('$PARALLEL_ID_1','$PARALLEL_ID_2')
      and status='running'
  ")
  if [ "$PARALLEL_STATE" = "2:2" ]; then
    PARALLEL_OBSERVED=1
    break
  fi
  sleep 0.1
done
assert_eq "$PARALLEL_OBSERVED" 1 "two workers hold different SKIP LOCKED claims"
curl -fsS -H "Cookie: $COOKIE_NAME=$TOKEN1" \
  "$BASE/api/v1/imports/$PARALLEL_ID_1" >"$WORK/parallel-running-get.json"
assert_json "$WORK/parallel-running-get.json" '.data.job.total_rows == 1 and .data.job.processed_rows == 0' "GET fallback reports staging total rows"
wait_batch "$PARALLEL_ID_1" succeeded
wait_batch "$PARALLEL_ID_2" succeeded
assert_eq "$(psqlq "select count(*) from job_queue where aggregate_id in ('$PARALLEL_ID_1','$PARALLEL_ID_2') and attempt_count=1 and lease_generation=1")" 2 "parallel jobs exactly once"
drop_retry_trigger
scale_workers 1

# A generation-1 executor is held inside its formal transaction. Its lease is
# expired and a second worker reclaims generation 2 while the stale writer is
# still running. Only generation 2 may commit or emit a terminal event.
printf 'date,code,name,value\n2026-08-06,OVERLAP1,Generation overlap,1\n' >"$WORK/overlap.csv"
OVERLAP_ID=$(prepare_batch "$WORK/overlap.csv" text/csv overlap)
stop_workers
assert_eq "$(confirm_batch "$OVERLAP_ID" skip phase3c-e2e-overlap-key-000001 "$WORK/overlap-confirm.json")" 202 "overlap confirm"
OVERLAP_JOB=$(jq -r '.data.job_id' "$WORK/overlap-confirm.json")
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_overlap_delay()
  returns trigger language plpgsql as \$\$
  declare current_generation bigint;
  begin
    if new.workspace_id = '$WS1'::uuid
       and new.business_key = '2026-08-06|OVERLAP1' then
      select lease_generation into current_generation
      from job_queue
      where workspace_id=new.workspace_id
        and aggregate_id=new.source_import_batch_id;
      if current_generation = 1 then
        perform pg_sleep(12);
      elsif current_generation = 2 then
        perform pg_sleep(3);
      end if;
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_overlap_delay
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_overlap_delay();
" >/dev/null
scale_workers 1
GENERATION_ONE_WORKER=
for _ in $(seq 1 100); do
  OVERLAP_STATE=$(psqlq "select status || ':' || lease_generation::text || ':' || coalesce(leased_by,'') from job_queue where id='$OVERLAP_JOB'")
  if [[ "$OVERLAP_STATE" == running:1:* ]]; then
    GENERATION_ONE_WORKER=${OVERLAP_STATE#running:1:}
    break
  fi
  sleep 0.1
done
test -n "$GENERATION_ONE_WORKER" || {
  echo "ASSERT_FAIL generation one executor was not observed" >&2
  exit 1
}
sleep 1
psqlq "update job_queue set lease_expires_at=now()-interval '1 second' where id='$OVERLAP_JOB' and status='running' and lease_generation=1" >/dev/null
scale_workers 2
GENERATION_TWO_OBSERVED=0
for _ in $(seq 1 100); do
  psqlq "update job_queue set lease_expires_at=now()-interval '1 second' where id='$OVERLAP_JOB' and status='running' and lease_generation=1" >/dev/null
  OVERLAP_STATE=$(psqlq "select status || ':' || lease_generation::text || ':' || coalesce(leased_by,'') from job_queue where id='$OVERLAP_JOB'")
  if [[ "$OVERLAP_STATE" == running:2:* ]] &&
     [ "${OVERLAP_STATE#running:2:}" != "$GENERATION_ONE_WORKER" ]; then
    GENERATION_TWO_OBSERVED=1
    break
  fi
  sleep 0.1
done
assert_eq "$GENERATION_TWO_OBSERVED" 1 "second worker reclaimed generation during stale execution"
wait_batch "$OVERLAP_ID" succeeded
sleep 12
assert_eq "$(psqlq "select status || ':' || attempt_count::text || ':' || lease_generation::text from job_queue where id='$OVERLAP_JOB'")" "succeeded:2:2" "generation two owns terminal state"
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$OVERLAP_ID'")" 1 "stale formal write rolled back"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$OVERLAP_JOB' and event_type='running'")" 2 "two generations observed"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$OVERLAP_JOB' and event_type='succeeded'")" 1 "one terminal success"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$OVERLAP_JOB' and event_type in ('failed','dead_letter')")" 0 "stale generation cannot terminate"
drop_retry_trigger
scale_workers 1

# Expired idle lease recovery advances the generation. A real 40001 is retried
# and succeeds after trap-safe trigger removal.
printf 'date,code,name,value\n2026-07-29,LEASE1,Lease recovery,1\n' >"$WORK/lease.csv"
LEASE_ID=$(prepare_batch "$WORK/lease.csv" text/csv lease)
stop_workers
assert_eq "$(confirm_batch "$LEASE_ID" skip phase3c-e2e-lease-00000001 "$WORK/lease-confirm.json")" 202 "lease confirm"
LEASE_JOB=$(jq -r '.data.job_id' "$WORK/lease-confirm.json")
psqlq "update job_queue set status='running',attempt_count=0,max_attempts=2,leased_by='dead-worker',lease_expires_at=now()-interval '1 minute' where id='$LEASE_JOB'; update import_batches set status='importing' where id='$LEASE_ID';" >/dev/null
scale_workers 1
wait_batch "$LEASE_ID" succeeded
assert_eq "$(psqlq "select attempt_count from job_queue where id='$LEASE_JOB'")" 1 "expired lease reclaimed"
assert_eq "$(psqlq "select lease_generation from job_queue where id='$LEASE_JOB'")" 1 "reclaim advances generation"

printf 'date,code,name,value\n2026-07-31,RETRY1,Transient retry,1\n' >"$WORK/retry.csv"
RETRY_ID=$(prepare_batch "$WORK/retry.csv" text/csv retry)
stop_workers
assert_eq "$(confirm_batch "$RETRY_ID" skip phase3c-e2e-retry-00000001 "$WORK/retry-confirm.json")" 202 "retry confirm"
RETRY_JOB=$(jq -r '.data.job_id' "$WORK/retry-confirm.json")
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_transient_failure()
  returns trigger language plpgsql as \$\$
  begin
    if new.workspace_id = '$WS1'::uuid and new.business_key = '2026-07-31|RETRY1' then
      raise exception 'phase3c e2e transient failure' using errcode = '40001';
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_transient_failure
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_transient_failure();
" >/dev/null
scale_workers 1
RETRY_OBSERVED=0
for _ in $(seq 1 100); do
  RETRY_STATE=$(psqlq "select status || ':' || attempt_count::text || ':' || coalesce(last_error_code,'') from job_queue where id='$RETRY_JOB'")
  if [ "$RETRY_STATE" = queued:1:database_error ]; then
    RETRY_OBSERVED=1
    break
  fi
  sleep 0.1
done
assert_eq "$RETRY_OBSERVED" 1 "transient failure queued for retry"
drop_retry_trigger
wait_batch "$RETRY_ID" succeeded
assert_eq "$(psqlq "select attempt_count from job_queue where id='$RETRY_JOB'")" 2 "transient retry second attempt"
assert_eq "$(psqlq "select lease_generation from job_queue where id='$RETRY_JOB'")" 2 "retry advances generation"
RETRY_LAST_EVENT=$(psqlq "select max(event_seq) from import_job_events where job_id='$RETRY_JOB'")
CROSS_BATCH_SSE_STATUS=$(curl -sS -o "$WORK/sse-cross-batch.json" -w '%{http_code}' \
  -H "Cookie: $COOKIE_NAME=$TOKEN1" -H "Last-Event-ID: $RETRY_LAST_EVENT" \
  "$BASE/api/v1/imports/$FULL_ID/events")
assert_eq "$CROSS_BATCH_SSE_STATUS" 400 "cross batch SSE cursor rejected"
assert_json "$WORK/sse-cross-batch.json" '.data.code == "event_id_invalid"' "cross batch SSE body"

# Five real serialization failures exercise every attempt and only the fifth
# transitions to dead_letter.
printf 'date,code,name,value\n2026-07-30,DEAD1,Five failures,1\n' >"$WORK/dead.csv"
DEAD_ID=$(prepare_batch "$WORK/dead.csv" text/csv dead)
stop_workers
assert_eq "$(confirm_batch "$DEAD_ID" skip phase3c-e2e-dead-000000001 "$WORK/dead-confirm.json")" 202 "dead letter confirm"
DEAD_JOB=$(jq -r '.data.job_id' "$WORK/dead-confirm.json")
drop_retry_trigger
psqlq "
  create function app.phase3c_e2e_always_failure()
  returns trigger language plpgsql as \$\$
  begin
    if new.workspace_id = '$WS1'::uuid
       and new.business_key = '2026-07-30|DEAD1' then
      raise exception 'phase3c e2e persistent serialization failure'
        using errcode = '40001';
    end if;
    return new;
  end
  \$\$;
  create trigger phase3c_e2e_always_failure
    before insert or update on imported_records
    for each row execute function app.phase3c_e2e_always_failure();
" >/dev/null
scale_workers 1
wait_job_sql "$DEAD_JOB" dead_letter
drop_retry_trigger
assert_eq "$(psqlq "select status::text from import_batches where id='$DEAD_ID'")" failed "dead letter batch failed"
assert_eq "$(psqlq "select attempt_count from job_queue where id='$DEAD_JOB'")" 5 "dead letter on fifth attempt"
assert_eq "$(psqlq "select lease_generation from job_queue where id='$DEAD_JOB'")" 5 "each failed claim has unique generation"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$DEAD_JOB' and event_type='running'")" 5 "five running attempts"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$DEAD_JOB' and event_type='progress' and payload->>'error_code'='database_error'")" 4 "four queued retries"
assert_eq "$(psqlq "select count(*) from import_job_events where job_id='$DEAD_JOB' and event_type='dead_letter'")" 1 "dead letter event"
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$DEAD_ID'")" 0 "failed attempts leave no formal rows"

AUDIT_TYPES=$(psqlq "select count(distinct event_type) from audit_logs where workspace_id='$WS1' and event_type in ('import.validate','import.confirmed','import.confirm_replayed','import.worker_succeeded','import.worker_dead_letter')")
assert_eq "$AUDIT_TYPES" 5 "audit coverage"
if "${COMPOSE_CMD[@]}" logs --no-color api worker |
  grep -E "$TOKEN1|$TOKEN2|$CSRF1|$CSRF2|$IDEMPOTENCY" >/dev/null; then
  echo "ASSERT_FAIL secret appeared in logs" >&2
  exit 1
fi
assert_eq "$(psqlq "select count(*) from audit_logs a where workspace_id='$WS1' and exists (select 1 from jsonb_object_keys(a.metadata) as k(key) where k.key in ('raw_value','idempotency_key','csrf','token','secret'))")" 0 "audit metadata redacted"

cat >"$WORK/result.env" <<EOF
WS1=$WS1
WS2=$WS2
FULL_ID=$FULL_ID
FULL_JOB=$FIRST_JOB
TRANSFORM_ID=$TRANSFORM_ID
FORMULA_ID=$FORMULA_ID
RENEWAL_ID=$RENEWAL_ID
RENEWAL_JOB=$RENEWAL_JOB
EXIT_ID=$EXIT_ID
EXIT_JOB=$EXIT_JOB
LEASE_ID=$LEASE_ID
LEASE_JOB=$LEASE_JOB
RETRY_ID=$RETRY_ID
RETRY_JOB=$RETRY_JOB
DEAD_ID=$DEAD_ID
DEAD_JOB=$DEAD_JOB
EOF

echo "PHASE3C_E2E_PASS"
echo "evidence_dir=$WORK"
echo "full_rows=75 concurrent_confirm=20 idempotency=4-combinations+cross-batch+single-commit policies=4 conflict_matrix=12/12 transforms=pass xlsx_formula=pass sse=replay+invalid+cross-batch errors=cursor rls=5/5-read+write skip_locked=two-workers lease_renewal=observed worker_restart=recovered generation_overlap=fenced permanent_abort=one-attempt transient_retry=pass dead_letter=fifth-attempt audit=pass secrets=clean"
