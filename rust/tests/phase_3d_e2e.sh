#!/usr/bin/env bash
# Phase 3D post-GHCR acceptance harness for the futures VPS.
#
# This script validates an already deployed candidate. It never compiles source
# and never pulls or changes images. Transfer this script, its sibling
# phase_3d_schema_invariants.sql, phase_3c_e2e.sh, and fixtures/phase3d_sample.xls
# as ephemeral E2E artifacts; the deployment root needs only Compose/deployment
# files and never needs a source checkout.
# Required non-secret release metadata:
#   PHASE3D_SOURCE_SHA=<40 hex commit>
#   PHASE3D_API_IMAGE=<lowercase ghcr.io image name>
#   PHASE3D_API_DIGEST=sha256:<64 hex>
#   PHASE3D_WORKER_IMAGE=... PHASE3D_WORKER_DIGEST=...
#   PHASE3D_FRONTEND_IMAGE=... PHASE3D_FRONTEND_DIGEST=...
#
# Operational inputs:
#   PHASE3D_ROOT=/opt/futures-platform
#   PHASE3D_BASE_URL=http://127.0.0.1:8088
#   PHASE3D_ORIGIN=http://localhost:8088
#   PHASE3D_COMPOSE="docker compose -f docker-compose.yml -f docker-compose.production.yml"
#   PHASE3D_DB_ADMIN_USER=<compose postgres administration user>
#   PHASE3D_DB_NAME=futures_platform
#   PHASE3D_ALLOW_SERVICE_RESTART=1
#
# A same-candidate Phase 3C regression must run immediately before this script.
# Transfer the tracked phase_3c_e2e.sh beside this script and provide:
#   PHASE3D_PHASE3C_ATTESTATION=/secure/evidence/phase3c-attestation.txt
#   PHASE3D_PHASE3C_STDOUT=/secure/evidence/phase3c-stdout.txt
#   PHASE3D_PHASE3C_RESULT_ENV=/secure/evidence/result.env
# The attestation is non-secret and contains exactly these facts:
#   PHASE3C_E2E_PASS
#   source_sha=<same PHASE3D_SOURCE_SHA>
#   script_sha256=<sha256 of sibling phase_3c_e2e.sh>
#   stdout_sha256=<sha256 of captured successful stdout>
#   result_env_sha256=<sha256 of PHASE3D_PHASE3C_RESULT_ENV>
#   completed_at_epoch=<UTC epoch written only after phase_3c_e2e.sh exits 0>
# One exact way to produce it without changing phase_3c_e2e.sh is:
#   evidence=/secure/evidence; source_sha=<candidate 40-hex SHA>
#   mkdir -p "$evidence"; chmod 700 "$evidence"
#   set -o pipefail
#   PHASE3C_WORK_DIR="$evidence/phase3c-work" \
#     bash phase_3c_e2e.sh | tee "$evidence/phase3c-stdout.txt"
#   test "${PIPESTATUS[0]}" -eq 0
#   grep -qx PHASE3C_E2E_PASS "$evidence/phase3c-stdout.txt"
#   { echo PHASE3C_E2E_PASS; echo "source_sha=$source_sha";
#     echo "script_sha256=$(sha256sum phase_3c_e2e.sh | awk '{print $1}')";
#     echo "stdout_sha256=$(sha256sum "$evidence/phase3c-stdout.txt" | awk '{print $1}')";
#     echo "result_env_sha256=$(sha256sum "$evidence/phase3c-work/result.env" | awk '{print $1}')";
#     echo "completed_at_epoch=$(date +%s)"; } >"$evidence/phase3c-attestation.txt"
# Phase 3C supplies four conflict policies, concurrency,
#   two-worker, lease/SIGKILL/generation fencing, retry, dead-letter, SSE/errors,
#   RLS, and Phase 1/2 regressions. This script independently exercises
#   TXT/XLS/XLSX format smoke plus every Phase 3D delta. A real legacy XLS
#   fixture is mandatory because generating BIFF is not a standard-library task.
#
# Credentials remain in the deployment's existing read-only secret mounts. The
# harness creates random, short-lived session material in memory and never
# prints it. Evidence intentionally contains no cookies, CSRF values, passwords,
# idempotency keys, database URLs, or absolute object paths.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ROOT=${PHASE3D_ROOT:-/opt/futures-platform}
BASE=${PHASE3D_BASE_URL:-http://127.0.0.1:8088}
ORIGIN=${PHASE3D_ORIGIN:-http://localhost:8088}
COOKIE_NAME=${PHASE3D_COOKIE_NAME:-futures_session}
DB_ADMIN_USER=${PHASE3D_DB_ADMIN_USER:-futures_app}
DB_NAME=${PHASE3D_DB_NAME:-futures_platform}
MIGRATION_DB_ROLE=${PHASE3D_MIGRATION_DB_ROLE:-futures_migrator}
RUNTIME_DB_ROLE=${PHASE3D_RUNTIME_DB_ROLE:-futures_runtime}
WORK=${PHASE3D_EVIDENCE_DIR:-/tmp/phase3d-e2e-evidence-$$}
ALLOW_RESTART=${PHASE3D_ALLOW_SERVICE_RESTART:-0}
PHASE3C_ATTESTATION=${PHASE3D_PHASE3C_ATTESTATION:?set PHASE3D_PHASE3C_ATTESTATION}
PHASE3C_STDOUT=${PHASE3D_PHASE3C_STDOUT:?set PHASE3D_PHASE3C_STDOUT}
PHASE3C_RESULT_ENV=${PHASE3D_PHASE3C_RESULT_ENV:?set PHASE3D_PHASE3C_RESULT_ENV}
PHASE3C_MAX_AGE_SECONDS=${PHASE3D_PHASE3C_MAX_AGE_SECONDS:-7200}
XLS_FIXTURE="$SCRIPT_DIR/fixtures/phase3d_sample.xls"
read -r -a COMPOSE_CMD <<<"${PHASE3D_COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.production.yml}"

SOURCE_SHA=${PHASE3D_SOURCE_SHA:?set PHASE3D_SOURCE_SHA}
API_IMAGE=${PHASE3D_API_IMAGE:?set PHASE3D_API_IMAGE}
API_DIGEST=${PHASE3D_API_DIGEST:?set PHASE3D_API_DIGEST}
WORKER_IMAGE=${PHASE3D_WORKER_IMAGE:?set PHASE3D_WORKER_IMAGE}
WORKER_DIGEST=${PHASE3D_WORKER_DIGEST:?set PHASE3D_WORKER_DIGEST}
FRONTEND_IMAGE=${PHASE3D_FRONTEND_IMAGE:?set PHASE3D_FRONTEND_IMAGE}
FRONTEND_DIGEST=${PHASE3D_FRONTEND_DIGEST:?set PHASE3D_FRONTEND_DIGEST}

for command in awk base64 cat cp curl date dirname docker find grep head jq mkdir \
  openssl paste python3 sed seq sha256sum sleep sort tail timeout tr wc
do
  command -v "$command" >/dev/null || {
    echo "PREREQ_FAIL missing_command=$command" >&2
    exit 1
  }
done
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "PREREQ_FAIL invalid_source_sha" >&2
  exit 1
}
for digest in "$API_DIGEST" "$WORKER_DIGEST" "$FRONTEND_DIGEST"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "PREREQ_FAIL invalid_image_digest" >&2
    exit 1
  }
done
for image in "$API_IMAGE" "$WORKER_IMAGE" "$FRONTEND_IMAGE"; do
  [ "$image" = "$(printf '%s' "$image" | tr '[:upper:]' '[:lower:]')" ] || {
    echo "PREREQ_FAIL image_name_not_lowercase" >&2
    exit 1
  }
done
[ "$MIGRATION_DB_ROLE" = futures_migrator ] &&
  [ "$RUNTIME_DB_ROLE" = futures_runtime ] || {
  echo "PREREQ_FAIL unexpected_database_role_name" >&2
  exit 1
}
[ "$ALLOW_RESTART" = 1 ] || {
  echo "PREREQ_FAIL set_PHASE3D_ALLOW_SERVICE_RESTART=1_for_controlled_restart_test" >&2
  exit 1
}
test -d "$ROOT" || {
  echo "PREREQ_FAIL deployment_root_missing" >&2
  exit 1
}
test -f "$SCRIPT_DIR/phase_3d_schema_invariants.sql" || {
  echo "PREREQ_FAIL schema_invariants_missing" >&2
  exit 1
}
test -f "$SCRIPT_DIR/phase_3c_e2e.sh" || {
  echo "PREREQ_FAIL phase3c_script_missing" >&2
  exit 1
}
test -f "$PHASE3C_ATTESTATION" && test -f "$PHASE3C_STDOUT" &&
  test -f "$PHASE3C_RESULT_ENV" || {
  echo "PREREQ_FAIL phase3c_evidence_missing" >&2
  exit 1
}
test -f "$XLS_FIXTURE" || {
  echo "PREREQ_FAIL tracked_legacy_xls_fixture_missing" >&2
  exit 1
}
XLS_FIXTURE_SHA=$(sha256sum "$XLS_FIXTURE" | awk '{print $1}')
[ "$XLS_FIXTURE_SHA" = f21ce3deb12b41a594bf554ce3d3cdffe0395a67054d0c99d8e1bfa706e03597 ] || {
  echo "PREREQ_FAIL tracked_legacy_xls_fixture_changed" >&2
  exit 1
}
cd "$ROOT"
"${COMPOSE_CMD[@]}" config --quiet
case "$WORK" in
  /|"$ROOT"|"$ROOT"/*)
    echo "PREREQ_FAIL unsafe_evidence_directory" >&2
    exit 1
    ;;
esac
if [ -e "$WORK" ] && [ -n "$(find "$WORK" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "PREREQ_FAIL evidence_directory_not_empty" >&2
  exit 1
fi
mkdir -p "$WORK"

new_uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
evidence_value() {
  local key=$1 file=$2
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
hash_token() {
  printf '%s' "$1" | openssl dgst -sha256 -binary |
    base64 | tr '+/' '-_' | tr -d '=\n'
}
assert_eq() {
  if [ "$1" != "$2" ]; then
    echo "ASSERT_FAIL label=$3" >&2
    exit 1
  fi
}
assert_json() {
  local file=$1 expression=$2 label=$3
  if ! jq -e "$expression" "$file" >/dev/null; then
    echo "ASSERT_FAIL label=$label" >&2
    exit 1
  fi
}
psql_admin() {
  "${COMPOSE_CMD[@]}" exec -T postgres \
    psql -X -U "$DB_ADMIN_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"
}
psqlq() { psql_admin -Atq -c "$1"; }
runtime_psqlq() {
  local workspace=$1 sql=$2
  psql_admin -Atq -c \
    "begin; set local role futures_runtime; set local app.current_workspace_id='$workspace'; $sql; rollback;"
}
service_database_user() {
  local service=$1
  "${COMPOSE_CMD[@]}" exec -T "$service" sh -ceu '
    secret_file=${DATABASE_URL_FILE:-/run/secrets/database-url}
    database_url=$(cat "$secret_file")
    authority=${database_url#*://}
    userinfo=${authority%%@*}
    username=${userinfo%%:*}
    unset database_url authority userinfo
    printf "%s\n" "$username"
  '
}
api_get() {
  local token=$1 path=$2 output=$3
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" "$BASE$path"
}
api_json() {
  local token=$1 csrf=$2 method=$3 path=$4 body=$5 output=$6
  curl -sS -o "$output" -w '%{http_code}' -X "$method" \
    -H "Cookie: $COOKIE_NAME=$token" \
    -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" \
    -H 'Content-Type: application/json' \
    --data "$body" "$BASE$path"
}
api_idempotent_json() {
  local token=$1 csrf=$2 method=$3 path=$4 body=$5 key=$6 output=$7
  curl -sS -o "$output" -w '%{http_code}' -X "$method" \
    -H "Cookie: $COOKIE_NAME=$token" \
    -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" \
    -H "Idempotency-Key: $key" \
    -H 'Content-Type: application/json' \
    --data "$body" "$BASE$path"
}
upload_file() {
  local token=$1 csrf=$2 file=$3 mime=$4 output=$5
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" \
    -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" \
    -F "file=@$file;type=$mime" "$BASE/api/v1/imports"
}
compensation_upload() {
  local token=$1 csrf=$2 original=$3 file=$4 reason=$5 key=$6 output=$7
  curl -sS -o "$output" -w '%{http_code}' \
    -H "Cookie: $COOKIE_NAME=$token" \
    -H "x-csrf-token: $csrf" \
    -H "Origin: $ORIGIN" \
    -H "Idempotency-Key: $key" \
    -F "file=@$file;type=text/csv" \
    -F "reason=$reason" \
    "$BASE/api/v1/imports/$original/compensations"
}

WS1=$(new_uuid)
WS2=$(new_uuid)
USER1=$(new_uuid)
USER2=$(new_uuid)
SESSION1=$(new_uuid)
SESSION2=$(new_uuid)
SSE_SESSION=$(new_uuid)
RUN_MARK=${WS1:0:8}
USERNAME1="phase3d-e2e-admin-$RUN_MARK"
USERNAME2="phase3d-e2e-viewer-${WS2:0:8}"
TOKEN1=$(openssl rand -hex 32)
TOKEN2=$(openssl rand -hex 32)
SSE_TOKEN=$(openssl rand -hex 32)
CSRF1=$(openssl rand -hex 32)
CSRF2=$(openssl rand -hex 32)
SSE_CSRF=$(openssl rand -hex 32)
TOKEN1_HASH=$(hash_token "$TOKEN1")
TOKEN2_HASH=$(hash_token "$TOKEN2")
SSE_TOKEN_HASH=$(hash_token "$SSE_TOKEN")
CSRF1_HASH=$(hash_token "$CSRF1")
CSRF2_HASH=$(hash_token "$CSRF2")
SSE_CSRF_HASH=$(hash_token "$SSE_CSRF")
CLEANED=0
P3C_WS1=
P3C_WS2=
P3C_USER1=
P3C_USER2=

# Refuse stale, cross-release, modified-script, or ambiguous Phase 3C evidence.
assert_eq "$(grep -cx 'PHASE3C_E2E_PASS' "$PHASE3C_ATTESTATION")" 1 "phase3c pass marker"
for key in source_sha script_sha256 stdout_sha256 result_env_sha256 completed_at_epoch; do
  assert_eq "$(grep -c "^${key}=" "$PHASE3C_ATTESTATION")" 1 "phase3c attestation field"
done
for key in WS1 WS2; do
  assert_eq "$(grep -c "^${key}=" "$PHASE3C_RESULT_ENV")" 1 "phase3c result field"
done
assert_eq "$(evidence_value source_sha "$PHASE3C_ATTESTATION")" "$SOURCE_SHA" "phase3c source sha"
PHASE3C_SCRIPT_SHA=$(sha256sum "$SCRIPT_DIR/phase_3c_e2e.sh" | awk '{print $1}')
assert_eq "$(evidence_value script_sha256 "$PHASE3C_ATTESTATION")" "$PHASE3C_SCRIPT_SHA" "phase3c script sha"
assert_eq "$(evidence_value stdout_sha256 "$PHASE3C_ATTESTATION")" \
  "$(sha256sum "$PHASE3C_STDOUT" | awk '{print $1}')" "phase3c stdout sha"
assert_eq "$(grep -c '^PHASE3C_E2E_PASS$' "$PHASE3C_STDOUT")" 1 "phase3c stdout pass"
assert_eq "$(evidence_value result_env_sha256 "$PHASE3C_ATTESTATION")" \
  "$(sha256sum "$PHASE3C_RESULT_ENV" | awk '{print $1}')" "phase3c result evidence sha"
PHASE3C_COMPLETED_AT=$(evidence_value completed_at_epoch "$PHASE3C_ATTESTATION")
[[ "$PHASE3C_COMPLETED_AT" =~ ^[0-9]+$ ]] &&
  [[ "$PHASE3C_MAX_AGE_SECONDS" =~ ^[0-9]+$ ]] || {
  echo "PREREQ_FAIL invalid_phase3c_completion_time" >&2
  exit 1
}
PHASE3C_AGE=$(($(date +%s) - PHASE3C_COMPLETED_AT))
if [ "$PHASE3C_AGE" -lt 0 ] || [ "$PHASE3C_AGE" -gt "$PHASE3C_MAX_AGE_SECONDS" ]; then
  echo "PREREQ_FAIL phase3c_evidence_not_recent" >&2
  exit 1
fi
P3C_WS1=$(evidence_value WS1 "$PHASE3C_RESULT_ENV")
P3C_WS2=$(evidence_value WS2 "$PHASE3C_RESULT_ENV")
for workspace in "$P3C_WS1" "$P3C_WS2"; do
  [[ "$workspace" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    echo "PREREQ_FAIL invalid_phase3c_workspace_evidence" >&2
    exit 1
  }
done
test "$P3C_WS1" != "$P3C_WS2" || {
  echo "PREREQ_FAIL duplicate_phase3c_workspace_evidence" >&2
  exit 1
}
cp "$PHASE3C_ATTESTATION" "$WORK/phase3c-attestation.txt"

object_file_count() {
  local workspace=$1
  "${COMPOSE_CMD[@]}" exec -T -e E2E_WORKSPACE="$workspace" api sh -ceu '
    case "$E2E_WORKSPACE" in
      ????????-????-????-????-????????????) ;;
      *) exit 64 ;;
    esac
    root=${OBJECT_STORAGE_ROOT:-/var/lib/futures-platform/objects}
    count=0
    for relative in "objects/$E2E_WORKSPACE" ".tmp/$E2E_WORKSPACE" "quarantine/$E2E_WORKSPACE"; do
      if [ -d "$root/$relative" ]; then
        current=$(find "$root/$relative" -type f | wc -l)
        count=$((count + current))
      fi
    done
    printf "%s\n" "$count"
  '
}
cleanup_object_files() {
  local workspace=$1
  "${COMPOSE_CMD[@]}" exec -T -e E2E_WORKSPACE="$workspace" api sh -ceu '
    case "$E2E_WORKSPACE" in
      ????????-????-????-????-????????????) ;;
      *) exit 64 ;;
    esac
    root=${OBJECT_STORAGE_ROOT:-/var/lib/futures-platform/objects}
    for relative in "objects/$E2E_WORKSPACE" ".tmp/$E2E_WORKSPACE" "quarantine/$E2E_WORKSPACE"; do
      target="$root/$relative"
      if [ -d "$target" ]; then
        find "$target" -depth -type f -delete
        find "$target" -depth -type d -empty -delete
      fi
    done
  '
}
cleanup_database() {
  local scope_ws1=${1:-$WS1}
  local scope_ws2=${2:-$WS2}
  local scope_user1=${3:-$USER1}
  local scope_user2=${4:-$USER2}
  psql_admin -q \
    -v ws1="$scope_ws1" -v ws2="$scope_ws2" \
    -v user1="$scope_user1" -v user2="$scope_user2" <<'SQL'
begin;
set local session_replication_role = replica;
create temporary table phase3d_cleanup_scope(workspace_id uuid primary key) on commit drop;
insert into phase3d_cleanup_scope values (:'ws1'::uuid), (:'ws2'::uuid);
do $cleanup$
declare
  target record;
begin
  for target in
    select table_schema, table_name
      from information_schema.columns
     where table_schema = 'public' and column_name = 'workspace_id'
  loop
    execute format(
      'delete from %I.%I where workspace_id in (select workspace_id from phase3d_cleanup_scope)',
      target.table_schema,
      target.table_name
    );
  end loop;
end
$cleanup$;
do $verify$
declare
  target record;
  remaining bigint;
begin
  for target in
    select table_schema, table_name
      from information_schema.columns
     where table_schema = 'public' and column_name = 'workspace_id'
  loop
    execute format(
      'select count(*) from %I.%I where workspace_id in (select workspace_id from phase3d_cleanup_scope)',
      target.table_schema,
      target.table_name
    ) into remaining;
    if remaining <> 0 then
      raise exception 'phase3d cleanup residue in %.%', target.table_schema, target.table_name;
    end if;
  end loop;
end
$verify$;
delete from security_events where actor_user_id in (:'user1'::uuid, :'user2'::uuid);
delete from sessions where user_id in (:'user1'::uuid, :'user2'::uuid);
delete from user_roles where user_id in (:'user1'::uuid, :'user2'::uuid);
delete from workspace_memberships where user_id in (:'user1'::uuid, :'user2'::uuid);
delete from workspaces where id in (:'ws1'::uuid, :'ws2'::uuid);
delete from users where id in (:'user1'::uuid, :'user2'::uuid);
commit;
SQL
}
cleanup() {
  local status=$?
  set +e
  "${COMPOSE_CMD[@]}" up -d --scale worker=1 worker >/dev/null 2>&1
  cleanup_database >/dev/null 2>&1
  cleanup_object_files "$WS1" >/dev/null 2>&1
  cleanup_object_files "$WS2" >/dev/null 2>&1
  if [ -n "$P3C_USER1" ] && [ -n "$P3C_USER2" ]; then
    cleanup_database "$P3C_WS1" "$P3C_WS2" "$P3C_USER1" "$P3C_USER2" >/dev/null 2>&1
    cleanup_object_files "$P3C_WS1" >/dev/null 2>&1
    cleanup_object_files "$P3C_WS2" >/dev/null 2>&1
  fi
  CLEANED=1
  if [ "$status" -ne 0 ]; then
    echo "PHASE3D_E2E_FAIL line=${BASH_LINENO[0]:-unknown}" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
P3C_OWNER_CANDIDATE1=$(psqlq "select owner_user_id from workspaces where id='$P3C_WS1'")
P3C_OWNER_CANDIDATE2=$(psqlq "select owner_user_id from workspaces where id='$P3C_WS2'")
for user_id in "$P3C_OWNER_CANDIDATE1" "$P3C_OWNER_CANDIDATE2"; do
  [[ "$user_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    echo "PREREQ_FAIL phase3c_retained_fixture_owner_missing" >&2
    exit 1
  }
done
test "$P3C_OWNER_CANDIDATE1" != "$P3C_OWNER_CANDIDATE2" || {
  echo "PREREQ_FAIL duplicate_phase3c_owner_evidence" >&2
  exit 1
}
assert_eq "$(psqlq "
  select count(*)
    from workspaces workspace
    join users owner on owner.id=workspace.owner_user_id
   where (
          (workspace.id='$P3C_WS1' and workspace.owner_user_id='$P3C_OWNER_CANDIDATE1')
       or (workspace.id='$P3C_WS2' and workspace.owner_user_id='$P3C_OWNER_CANDIDATE2')
   )
     and workspace.name like 'Phase 3C E2E %'
     and owner.username like 'phase3c-e2e-%'
")" 2 "phase3c retained fixture identity"
P3C_USER1=$P3C_OWNER_CANDIDATE1
P3C_USER2=$P3C_OWNER_CANDIDATE2

wait_health() {
  for _ in $(seq 1 120); do
    if curl -fsS "$BASE/api/v1/ready" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "ASSERT_FAIL label=service_health_timeout" >&2
  exit 1
}
wait_batch() {
  local token=$1 import_id=$2 expected=$3
  local output="$WORK/wait-$import_id.json"
  for _ in $(seq 1 360); do
    if [ "$(api_get "$token" "/api/v1/imports/$import_id" "$output")" = 200 ]; then
      local status
      status=$(jq -r '.data.status' "$output")
      [ "$status" = "$expected" ] && return 0
      case "$status" in failed|dead_letter|rollback_conflict|rollback_failed)
        echo "ASSERT_FAIL label=batch_terminal_status" >&2; exit 1 ;;
      esac
    fi
    sleep 0.25
  done
  echo "ASSERT_FAIL label=batch_timeout" >&2
  exit 1
}
wait_governance_job() {
  local job_id=$1 expected=$2
  for _ in $(seq 1 360); do
    [ "$(psqlq "select status from object_governance_jobs where workspace_id='$WS1' and id='$job_id'")" = "$expected" ] && return 0
    sleep 0.25
  done
  echo "ASSERT_FAIL label=governance_job_timeout" >&2
  exit 1
}
DEFAULT_MAPPING='{"dataset_type":"generic","template_version_id":null,"fields":[{"source_column":"date","target_field":"trade_date","transform":"trim"},{"source_column":"code","target_field":"code","transform":"trim"},{"source_column":"name","target_field":"name","transform":"trim"},{"source_column":"value","target_field":"value","transform":"trim"}]}'
prepare_batch() {
  local token=$1 csrf=$2 file=$3 label=$4 mime=${5:-text/csv}
  local output="$WORK/$label-upload.json" import_id
  assert_eq "$(upload_file "$token" "$csrf" "$file" "$mime" "$output")" 201 "$label upload"
  import_id=$(jq -r '.data.id' "$output")
  assert_eq "$(api_json "$token" "$csrf" POST "/api/v1/imports/$import_id/inspect" '{}' "$WORK/$label-inspect.json")" 200 "$label inspect"
  assert_eq "$(api_json "$token" "$csrf" PUT "/api/v1/imports/$import_id/mapping" "$DEFAULT_MAPPING" "$WORK/$label-mapping.json")" 200 "$label mapping"
  assert_eq "$(api_json "$token" "$csrf" POST "/api/v1/imports/$import_id/preview" '{}' "$WORK/$label-preview.json")" 200 "$label preview"
  assert_eq "$(api_json "$token" "$csrf" POST "/api/v1/imports/$import_id/validate" '{}' "$WORK/$label-validate.json")" 200 "$label validate"
  assert_json "$WORK/$label-validate.json" '.data.blocking_error_count == 0' "$label validation"
  printf '%s' "$import_id"
}
confirm_batch() {
  local token=$1 csrf=$2 import_id=$3 key=$4 output=$5 policy=${6:-skip}
  api_idempotent_json "$token" "$csrf" POST "/api/v1/imports/$import_id/confirm" \
    "{\"conflict_policy\":\"$policy\"}" "$key" "$output"
}
rollback_check() {
  local import_id=$1 output=$2
  api_json "$TOKEN1" "$CSRF1" POST "/api/v1/imports/$import_id/rollback-check" '{}' "$output"
}
rollback_request() {
  local import_id=$1 check_file=$2 key=$3 output=$4 fingerprint_override=${5:-}
  local request_id fingerprint
  request_id=$(jq -r '.data.precheck_request_id' "$check_file")
  fingerprint=$(jq -r '.data.precheck_fingerprint' "$check_file")
  [ -z "$fingerprint_override" ] || fingerprint=$fingerprint_override
  api_idempotent_json "$TOKEN1" "$CSRF1" POST "/api/v1/imports/$import_id/rollback" \
    "{\"precheck_request_id\":\"$request_id\",\"precheck_fingerprint\":\"$fingerprint\"}" \
    "$key" "$output"
}
database_snapshot() {
  local import_id=$1
  psqlq "select count(*)::text || ':' || coalesce(md5(string_agg(id::text || row_version::text || record_data::text,'|' order by id)),'empty') from imported_records where workspace_id='$WS1' and source_import_batch_id='$import_id'"
}

# Release identity: configured digest must resolve to the running container image,
# and every custom image must carry the requested immutable source revision.
for spec in \
  "api:$API_IMAGE:$API_DIGEST" \
  "worker:$WORKER_IMAGE:$WORKER_DIGEST" \
  "frontend:$FRONTEND_IMAGE:$FRONTEND_DIGEST"
do
  service=${spec%%:*}
  remainder=${spec#*:}
  digest=${remainder##*:sha256:}
  digest="sha256:$digest"
  image=${remainder%:sha256:*}
  expected_id=$(docker image inspect --format '{{.Id}}' "$image@$digest")
  container_id=$("${COMPOSE_CMD[@]}" ps -q "$service" | head -n 1)
  test -n "$container_id" || { echo "ASSERT_FAIL label=service_not_running" >&2; exit 1; }
  assert_eq "$(docker inspect --format '{{.Image}}' "$container_id")" "$expected_id" "$service digest"
  assert_eq "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$expected_id")" "$SOURCE_SHA" "$service revision label"
done
curl -fsS "$BASE/api/v1/version" >"$WORK/version.json"
assert_json "$WORK/version.json" ".data.git_sha == \"$SOURCE_SHA\" and .data.git_sha != \"local\"" "real GIT_SHA"

# The actual API/Worker connection account and migration account must be LOGIN
# roles and must be powerless to bypass RLS.
assert_eq "$(service_database_user api)" "$RUNTIME_DB_ROLE" "API runtime database account"
assert_eq "$(service_database_user worker)" "$RUNTIME_DB_ROLE" "Worker runtime database account"
assert_eq "$(psqlq "select count(*) from pg_roles where rolname in ('$MIGRATION_DB_ROLE','$RUNTIME_DB_ROLE') and rolcanlogin and not rolsuper and not rolbypassrls")" 2 "database accounts non privileged"
psql_admin -Atq -f "$SCRIPT_DIR/phase_3d_schema_invariants.sql" >"$WORK/schema-invariants.txt"
grep -qx 'PHASE3D_SCHEMA_INVARIANTS_PASS' "$WORK/schema-invariants.txt"
psql_admin -Atq -c "select version || ' ' || description from schema_versions order by version" >"$WORK/migrations.txt"
grep -q '^202607260001 ' "$WORK/migrations.txt"
grep -q '^202607260002 ' "$WORK/migrations.txt"

# Isolated administrators/viewers and three short-lived sessions.
psql_admin -q <<SQL
insert into users(id,username,username_normalized,password_hash,password_params_version)
values
  ('$USER1','$USERNAME1','$USERNAME1','unused-phase3d-e2e-hash',1),
  ('$USER2','$USERNAME2','$USERNAME2','unused-phase3d-e2e-hash',1);
insert into workspaces(id,name,owner_user_id)
values
  ('$WS1','Phase 3D E2E $RUN_MARK','$USER1'),
  ('$WS2','Phase 3D E2E isolated ${WS2:0:8}','$USER2');
insert into workspace_memberships(id,workspace_id,user_id,role)
values
  ('$(new_uuid)','$WS1','$USER1','owner'),
  ('$(new_uuid)','$WS2','$USER2','owner');
insert into user_roles(user_id,role_name)
values ('$USER1','admin'),('$USER2','viewer');
insert into sessions
  (id,user_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at,user_agent)
values
  ('$SESSION1','$USER1','$TOKEN1_HASH','$CSRF1_HASH',now()+interval '2 hours',now()+interval '2 hours','phase3d-e2e'),
  ('$SESSION2','$USER2','$TOKEN2_HASH','$CSRF2_HASH',now()+interval '2 hours',now()+interval '2 hours','phase3d-e2e'),
  ('$SSE_SESSION','$USER1','$SSE_TOKEN_HASH','$SSE_CSRF_HASH',now()+interval '2 hours',now()+interval '2 hours','phase3d-e2e-sse');
SQL

# Current-candidate format smoke. CSV is exercised throughout the Phase 3D
# scenarios; these three batches prevent PASS when TXT/XLS/XLSX regresses.
printf 'date\tcode\tname\tvalue\n2027-03-01\tP3D-TXT1\tText smoke\t1\n' >"$WORK/format.txt"
TXT_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/format.txt" format-txt text/plain)
assert_json "$WORK/format-txt-upload.json" '.data.file.detected_format == "txt"' "TXT detected"
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$TXT_ID" "phase3d-confirm-$RUN_MARK-txt" "$WORK/format-txt-confirm.json")" 202 "TXT confirm"
wait_batch "$TOKEN1" "$TXT_ID" succeeded
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$TXT_ID'")" 1 "TXT formal row"

XLS_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$XLS_FIXTURE" format-xls application/vnd.ms-excel)
assert_json "$WORK/format-xls-upload.json" '.data.file.detected_format == "xls"' "XLS detected"
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$XLS_ID" "phase3d-confirm-$RUN_MARK-xls" "$WORK/format-xls-confirm.json")" 202 "XLS confirm"
wait_batch "$TOKEN1" "$XLS_ID" succeeded
assert_eq "$(psqlq "select (count(*) > 0)::text from imported_records where workspace_id='$WS1' and source_import_batch_id='$XLS_ID'")" true "XLS formal rows"

python3 - "$WORK/format.xlsx" <<'PY'
import sys
import zipfile

output = sys.argv[1]
parts = {
    "[Content_Types].xml": """<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>""",
    "_rels/.rels": """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""",
    "xl/workbook.xml": """<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>""",
    "xl/_rels/workbook.xml.rels": """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>""",
    "xl/worksheets/sheet1.xml": """<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>date</t></is></c><c r="B1" t="inlineStr"><is><t>code</t></is></c><c r="C1" t="inlineStr"><is><t>name</t></is></c><c r="D1" t="inlineStr"><is><t>value</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>2027-03-02</t></is></c><c r="B2" t="inlineStr"><is><t>P3D-XLSX1</t></is></c><c r="C2" t="inlineStr"><is><t>XLSX smoke</t></is></c><c r="D2" t="inlineStr"><is><t>2</t></is></c></row></sheetData></worksheet>""",
}
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, data in parts.items():
        archive.writestr(name, data)
PY
XLSX_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/format.xlsx" format-xlsx application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)
assert_json "$WORK/format-xlsx-upload.json" '.data.file.detected_format == "xlsx"' "XLSX detected"
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$XLSX_ID" "phase3d-confirm-$RUN_MARK-xlsx" "$WORK/format-xlsx-confirm.json")" 202 "XLSX confirm"
wait_batch "$TOKEN1" "$XLSX_ID" succeeded
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$XLSX_ID'")" 1 "XLSX formal row"

# Direct imports produce a complete, versioned change log. The candidate updates
# one prior row and inserts one row; rollback must restore the former snapshot and
# remove the latter, proving inverse recovery rather than merely status changes.
cat >"$WORK/baseline.csv" <<'CSV'
date,code,name,value
2026-09-01,P3D-D1,Baseline,5
CSV
BASELINE_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/baseline.csv" baseline)
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$BASELINE_ID" "phase3d-confirm-$RUN_MARK-baseline" "$WORK/baseline-confirm.json")" 202 "baseline confirm"
wait_batch "$TOKEN1" "$BASELINE_ID" succeeded
cat >"$WORK/direct.csv" <<'CSV'
date,code,name,value
2026-09-01,P3D-D1,Direct one,10
2026-09-02,P3D-D2,Direct two,20
CSV
DIRECT_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/direct.csv" direct)
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$DIRECT_ID" "phase3d-confirm-$RUN_MARK-direct" "$WORK/direct-confirm.json" overwrite)" 202 "direct confirm"
wait_batch "$TOKEN1" "$DIRECT_ID" succeeded
assert_eq "$(psqlq "select rollback_capability || ':' || change_log_version::text from import_batches where workspace_id='$WS1' and id='$DIRECT_ID'")" "direct:1" "direct change log capability"
assert_eq "$(psqlq "select count(*) from import_row_changes where workspace_id='$WS1' and import_batch_id='$DIRECT_ID'")" 2 "direct change log count"

# Precheck pagination: 105 later modifications produce a complete second page.
printf 'date,code,name,value\n' >"$WORK/conflicts.csv"
for i in $(seq -w 1 105); do
  printf '2026-10-01,P3D-C%s,Conflict %s,%s\n' "$i" "$i" "$((10#$i))" >>"$WORK/conflicts.csv"
done
CONFLICT_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/conflicts.csv" conflicts)
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$CONFLICT_ID" "phase3d-confirm-$RUN_MARK-conflicts" "$WORK/conflicts-confirm.json")" 202 "conflict batch confirm"
wait_batch "$TOKEN1" "$CONFLICT_ID" succeeded
psqlq "update imported_records set record_data=jsonb_set(record_data,'{value}','\"changed\"'),row_version=row_version+1 where workspace_id='$WS1' and source_import_batch_id='$CONFLICT_ID'" >/dev/null
CONFLICT_BEFORE=$(database_snapshot "$CONFLICT_ID")
assert_eq "$(rollback_check "$CONFLICT_ID" "$WORK/conflict-check.json")" 200 "conflict precheck"
assert_json "$WORK/conflict-check.json" '.data.can_rollback == false and .data.conflict_count == 105 and (.data.conflicts|length) == 100 and (.data.next_cursor|type) == "string"' "conflict precheck page one"
PRECHECK_ID=$(jq -r '.data.precheck_request_id' "$WORK/conflict-check.json")
CURSOR=$(jq -r '.data.next_cursor' "$WORK/conflict-check.json")
assert_eq "$(api_get "$TOKEN1" "/api/v1/imports/$CONFLICT_ID/rollback-conflicts?precheck_request_id=$PRECHECK_ID&cursor=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$CURSOR")" "$WORK/conflict-page-2.json")" 200 "conflict page two"
assert_json "$WORK/conflict-page-2.json" '(.data.items|length) == 5 and .data.next_cursor == null' "conflict page two complete"
assert_eq "$(rollback_request "$CONFLICT_ID" "$WORK/conflict-check.json" "phase3d-rollback-$RUN_MARK-conflict" "$WORK/conflict-rollback.json")" 409 "conflicted rollback rejected"
assert_json "$WORK/conflict-rollback.json" '.data.code == "rollback_conflict"' "conflicted rollback code"
assert_eq "$(database_snapshot "$CONFLICT_ID")" "$CONFLICT_BEFORE" "later modification zero business change"

# Same-key replay, same-key/different-parameters rejection, and twenty concurrent
# rollback submissions converge while the worker is stopped.
assert_eq "$(rollback_check "$DIRECT_ID" "$WORK/direct-check.json")" 200 "direct rollback check"
assert_json "$WORK/direct-check.json" '.data.can_rollback == true and .data.affected_count == 2 and .data.conflict_count == 0' "direct rollback allowed"
"${COMPOSE_CMD[@]}" stop worker >/dev/null
ROLLBACK_KEY="phase3d-rollback-$RUN_MARK-concurrent"
PIDS=()
for i in $(seq 1 20); do
  (rollback_request "$DIRECT_ID" "$WORK/direct-check.json" "$ROLLBACK_KEY" "$WORK/rollback-$i.json" >"$WORK/rollback-$i.status") &
  PIDS+=("$!")
done
for pid in "${PIDS[@]}"; do wait "$pid"; done
ROLLBACK_JOB=$(jq -r '.data.job_id' "$WORK/rollback-1.json")
REPLAYS=0
for i in $(seq 1 20); do
  assert_eq "$(cat "$WORK/rollback-$i.status")" 202 "concurrent rollback"
  assert_eq "$(jq -r '.data.job_id' "$WORK/rollback-$i.json")" "$ROLLBACK_JOB" "single rollback job"
  [ "$(jq -r '.data.replayed' "$WORK/rollback-$i.json")" = true ] && REPLAYS=$((REPLAYS + 1))
done
assert_eq "$REPLAYS" 19 "rollback replay count"
assert_eq "$(rollback_request "$DIRECT_ID" "$WORK/direct-check.json" "$ROLLBACK_KEY" "$WORK/rollback-key-reused.json" "$(printf 'f%.0s' {1..64})")" 409 "rollback same key different parameters"
assert_json "$WORK/rollback-key-reused.json" '.data.code == "rollback_idempotency_key_reused"' "rollback idempotency mismatch code"
"${COMPOSE_CMD[@]}" up -d --scale worker=1 worker >/dev/null
wait_batch "$TOKEN1" "$DIRECT_ID" rolled_back
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$DIRECT_ID'")" 0 "rollback inverse restore"
assert_eq "$(psqlq "select (record_data->>'value') || ':' || source_import_batch_id::text from imported_records where workspace_id='$WS1' and business_key='2026-09-01|P3D-D1'")" "5:$BASELINE_ID" "rollback restores prior source and value"
assert_eq "$(psqlq "select count(*) from import_data_invalidations where workspace_id='$WS1' and import_batch_id='$DIRECT_ID'")" 2 "rollback invalidation count"
assert_eq "$(psqlq "select status from import_rollback_requests where workspace_id='$WS1' and import_batch_id='$DIRECT_ID' and job_id='$ROLLBACK_JOB'")" succeeded "rollback atomic terminal"

# A syntactically valid stale fingerprint is rejected before any business write.
cat >"$WORK/stale.csv" <<'CSV'
date,code,name,value
2026-11-01,P3D-S1,Stale one,1
CSV
STALE_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/stale.csv" stale)
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$STALE_ID" "phase3d-confirm-$RUN_MARK-stale" "$WORK/stale-confirm.json")" 202 "stale batch confirm"
wait_batch "$TOKEN1" "$STALE_ID" succeeded
assert_eq "$(rollback_check "$STALE_ID" "$WORK/stale-check.json")" 200 "stale precheck"
STALE_BEFORE=$(database_snapshot "$STALE_ID")
assert_eq "$(rollback_request "$STALE_ID" "$WORK/stale-check.json" "phase3d-rollback-$RUN_MARK-stale" "$WORK/stale-result.json" "$(printf '0%.0s' {1..64})")" 409 "stale fingerprint rejected"
assert_json "$WORK/stale-result.json" '.data.code == "rollback_precondition_stale"' "stale fingerprint code"
assert_eq "$(database_snapshot "$STALE_ID")" "$STALE_BEFORE" "stale precheck zero business change"

# Compensation is a new batch with a complete import lifecycle and lineage.
cat >"$WORK/original.csv" <<'CSV'
date,code,name,value
2026-12-01,P3D-O1,Original,100
CSV
ORIGINAL_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/original.csv" original)
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$ORIGINAL_ID" "phase3d-confirm-$RUN_MARK-original" "$WORK/original-confirm.json")" 202 "original confirm"
wait_batch "$TOKEN1" "$ORIGINAL_ID" succeeded
cat >"$WORK/compensation.csv" <<'CSV'
date,code,name,value
2026-12-01,P3D-O1,Corrected,101
CSV
COMP_KEY="phase3d-compensation-$RUN_MARK"
assert_eq "$(compensation_upload "$TOKEN1" "$CSRF1" "$ORIGINAL_ID" "$WORK/compensation.csv" "phase3d corrective batch $RUN_MARK" "$COMP_KEY" "$WORK/compensation-upload.json")" 201 "compensation upload"
COMP_ID=$(jq -r '.data.compensation_import_id' "$WORK/compensation-upload.json")
assert_eq "$(api_json "$TOKEN1" "$CSRF1" POST "/api/v1/imports/$COMP_ID/inspect" '{}' "$WORK/compensation-inspect.json")" 200 "compensation inspect"
assert_eq "$(api_json "$TOKEN1" "$CSRF1" PUT "/api/v1/imports/$COMP_ID/mapping" "$DEFAULT_MAPPING" "$WORK/compensation-mapping.json")" 200 "compensation mapping"
assert_eq "$(api_json "$TOKEN1" "$CSRF1" POST "/api/v1/imports/$COMP_ID/preview" '{}' "$WORK/compensation-preview.json")" 200 "compensation preview"
assert_eq "$(api_json "$TOKEN1" "$CSRF1" POST "/api/v1/imports/$COMP_ID/validate" '{}' "$WORK/compensation-validate.json")" 200 "compensation validate"
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$COMP_ID" "phase3d-confirm-$RUN_MARK-compensation" "$WORK/compensation-confirm.json" overwrite)" 202 "compensation confirm"
wait_batch "$TOKEN1" "$COMP_ID" succeeded
assert_eq "$(psqlq "select (record_data->>'value') || ':' || source_import_batch_id::text from imported_records where workspace_id='$WS1' and business_key='2026-12-01|P3D-O1'")" "101:$COMP_ID" "compensation applies corrected value and source"
assert_eq "$(api_get "$TOKEN1" "/api/v1/imports/$COMP_ID/lineage" "$WORK/lineage.json")" 200 "lineage"
assert_json "$WORK/lineage.json" ".data.root_import_id == \"$ORIGINAL_ID\" and (.data.nodes|length) == 2 and ([.data.nodes[].import_id]|index(\"$COMP_ID\") != null) and ([.data.audits[].event_type]|index(\"import.compensation_created\") != null)" "compensation lineage complete"
DEPENDENCY_BEFORE=$(database_snapshot "$ORIGINAL_ID")
assert_eq "$(rollback_check "$ORIGINAL_ID" "$WORK/dependency-check.json")" 200 "dependency rollback check"
assert_json "$WORK/dependency-check.json" '.data.can_rollback == false and .data.compensation_recommended == true and ([.data.conflicts[].conflict_type]|index("downstream_dependency") != null)' "dependency blocks rollback"
assert_eq "$(rollback_request "$ORIGINAL_ID" "$WORK/dependency-check.json" "phase3d-rollback-$RUN_MARK-dependency" "$WORK/dependency-rollback.json")" 409 "dependency rollback rejected"
assert_json "$WORK/dependency-rollback.json" '.data.code == "rollback_conflict"' "dependency rollback code"
assert_eq "$(database_snapshot "$ORIGINAL_ID")" "$DEPENDENCY_BEFORE" "dependency zero business change"

# Cross-workspace API and forced-RLS probes.
assert_eq "$(api_get "$TOKEN2" "/api/v1/imports/$ORIGINAL_ID" "$WORK/cross-workspace-api.json")" 404 "cross workspace API invisible"
assert_json "$WORK/cross-workspace-api.json" '.data.code == "import_not_found"' "cross workspace API body"
assert_eq "$(runtime_psqlq "$WS2" "select count(*) from import_batches where id='$ORIGINAL_ID'")" 0 "cross workspace RLS read"
if runtime_psqlq "$WS2" "insert into import_batches(id,workspace_id,status,created_by) values ('$(new_uuid)','$WS1','uploaded','$USER1')" >/dev/null 2>&1; then
  echo "ASSERT_FAIL label=cross_workspace_RLS_write" >&2
  exit 1
fi

# Session revocation terminates an otherwise idle SSE stream after server-side
# revalidation. The response body is discarded and never captures a cookie.
cat >"$WORK/sse.csv" <<'CSV'
date,code,name,value
2027-01-01,P3D-E1,SSE idle,1
CSV
SSE_ID=$(prepare_batch "$SSE_TOKEN" "$SSE_CSRF" "$WORK/sse.csv" sse)
timeout 45 curl -fsSN -o "$WORK/sse-events.txt" \
  -H "Cookie: $COOKIE_NAME=$SSE_TOKEN" "$BASE/api/v1/imports/$SSE_ID/events" &
SSE_PID=$!
sleep 2
psqlq "update sessions set revoked_at=now() where id='$SSE_SESSION'" >/dev/null
for _ in $(seq 1 45); do
  if ! kill -0 "$SSE_PID" 2>/dev/null; then break; fi
  sleep 1
done
if kill -0 "$SSE_PID" 2>/dev/null; then
  kill "$SSE_PID" 2>/dev/null || true
  echo "ASSERT_FAIL label=revoked_session_SSE_termination" >&2
  exit 1
fi
wait "$SSE_PID" || true
assert_eq "$(psqlq "select count(*) from audit_logs where workspace_id='$WS1' and event_type='import.events_access_terminated' and metadata->>'reason_code'='auth_required'")" 1 "SSE revocation audit"

# Create a real orphan under the test workspace only, scan it, and quarantine it.
# The governance implementation must move it and must never physically delete it.
"${COMPOSE_CMD[@]}" exec -T -e E2E_WORKSPACE="$WS1" api sh -ceu '
  case "$E2E_WORKSPACE" in
    ????????-????-????-????-????????????) ;;
    *) exit 64 ;;
  esac
  root=${OBJECT_STORAGE_ROOT:-/var/lib/futures-platform/objects}
  target="$root/objects/$E2E_WORKSPACE/e2/e2/phase3d-e2e-orphan"
  mkdir -p "$(dirname "$target")"
  printf "phase3d orphan fixture\n" >"$target"
'
OBJECT_COUNT_BEFORE_SCAN=$(object_file_count "$WS1")
SCAN_KEY="phase3d-object-scan-$RUN_MARK"
assert_eq "$(api_idempotent_json "$TOKEN1" "$CSRF1" POST "/api/v1/object-consistency/scans" '{}' "$SCAN_KEY" "$WORK/object-scan.json")" 202 "object scan queued"
SCAN_RUN=$(jq -r '.data.run_id' "$WORK/object-scan.json")
SCAN_JOB=$(jq -r '.data.job_id' "$WORK/object-scan.json")
wait_governance_job "$SCAN_JOB" succeeded
assert_eq "$(api_get "$TOKEN1" "/api/v1/object-consistency/scans/$SCAN_RUN" "$WORK/object-report.json")" 200 "object scan report"
assert_json "$WORK/object-report.json" '.data.run.status == "completed" and ([.data.findings[]|select(.finding_type=="orphan_object" and .quarantine_eligible==true)]|length) == 1' "orphan finding"
FINDING_ID=$(jq -r '.data.findings[]|select(.finding_type=="orphan_object" and .quarantine_eligible==true)|.finding_id' "$WORK/object-report.json")
QUARANTINE_KEY="phase3d-quarantine-$RUN_MARK"
assert_eq "$(api_idempotent_json "$TOKEN1" "$CSRF1" POST "/api/v1/object-consistency/findings/$FINDING_ID/quarantine" '{}' "$QUARANTINE_KEY" "$WORK/quarantine.json")" 202 "quarantine queued"
QUARANTINE_JOB=$(jq -r '.data.job_id' "$WORK/quarantine.json")
wait_governance_job "$QUARANTINE_JOB" succeeded
assert_eq "$(psqlq "select count(*) from object_quarantines where workspace_id='$WS1' and finding_id='$FINDING_ID' and disposition_status='quarantined'")" 1 "quarantine record"
assert_eq "$(object_file_count "$WS1")" "$OBJECT_COUNT_BEFORE_SCAN" "governance physical delete count zero"

# Persistence/recovery uses the deployed images only. A queued import survives
# controlled PostgreSQL, API, worker, and edge restarts.
cat >"$WORK/restart.csv" <<'CSV'
date,code,name,value
2027-02-01,P3D-R1,Restart recovery,1
CSV
RESTART_ID=$(prepare_batch "$TOKEN1" "$CSRF1" "$WORK/restart.csv" restart)
"${COMPOSE_CMD[@]}" stop worker >/dev/null
assert_eq "$(confirm_batch "$TOKEN1" "$CSRF1" "$RESTART_ID" "phase3d-confirm-$RUN_MARK-restart" "$WORK/restart-confirm.json")" 202 "restart batch confirm"
"${COMPOSE_CMD[@]}" restart postgres api worker nginx >/dev/null
wait_health
wait_batch "$TOKEN1" "$RESTART_ID" succeeded
assert_eq "$(psqlq "select count(*) from imported_records where workspace_id='$WS1' and source_import_batch_id='$RESTART_ID'")" 1 "restart recovery persisted"

# Audit/event coverage and secret hygiene. Search actual ephemeral material plus
# high-confidence credential/private-key patterns without printing matches.
assert_eq "$(psqlq "select count(distinct event_type) from audit_logs where workspace_id='$WS1' and event_type in ('import.rollback_check','import.rollback_queued','import.rollback_worker_succeeded','import.compensation_created','object.scan_queued','object.scan_completed','object.quarantine_queued','object.quarantined','import.events_access_terminated')")" 9 "Phase 3D audit coverage"
assert_eq "$(psqlq "select count(*) from audit_logs where workspace_id='$WS1' and exists (select 1 from jsonb_object_keys(metadata) key where key in ('password','token','cookie','csrf','idempotency_key','object_key','raw_value','secret'))")" 0 "audit metadata secret free"
assert_eq "$(psqlq "select count(*) from import_job_events where workspace_id='$WS1' and event_type in ('rollback_queued','rollback_running','rolled_back')")" 3 "rollback event chain"
"${COMPOSE_CMD[@]}" logs --no-color --since 30m api worker nginx >"$WORK/service.log"
for secret in "$TOKEN1" "$TOKEN2" "$SSE_TOKEN" "$CSRF1" "$CSRF2" "$SSE_CSRF" \
  "$ROLLBACK_KEY" "$COMP_KEY" "$SCAN_KEY" "$QUARANTINE_KEY"; do
  if grep -F "$secret" "$WORK/service.log" "$WORK"/*.json "$WORK"/*.txt >/dev/null 2>&1; then
    echo "ASSERT_FAIL label=ephemeral_secret_in_evidence_or_logs" >&2
    exit 1
  fi
done
if grep -E -i \
  '(BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY|authorization:[[:space:]]*bearer|cookie:[[:space:]]*[^[:space:]]+|password[[:space:]]*[=:][[:space:]]*[^[:space:]]+)' \
  "$WORK/service.log" "$WORK"/*.json "$WORK"/*.txt \
  "$PHASE3C_STDOUT" "$PHASE3C_RESULT_ENV" >/dev/null 2>&1; then
  echo "ASSERT_FAIL label=credential_pattern_in_evidence_or_logs" >&2
  exit 1
fi

# Save only non-secret, portable release evidence.
SCRIPT_SHA=$(sha256sum "$SCRIPT_DIR/phase_3d_e2e.sh" | awk '{print $1}')
cat >"$WORK/release-summary.txt" <<EOF
source_sha=$SOURCE_SHA
script_sha256=$SCRIPT_SHA
phase3c_script_sha256=$PHASE3C_SCRIPT_SHA
phase3c_attestation_sha256=$(sha256sum "$PHASE3C_ATTESTATION" | awk '{print $1}')
xls_fixture_sha256=$XLS_FIXTURE_SHA
api_image=$API_IMAGE
api_digest=$API_DIGEST
worker_image=$WORKER_IMAGE
worker_digest=$WORKER_DIGEST
frontend_image=$FRONTEND_IMAGE
frontend_digest=$FRONTEND_DIGEST
migration_count=$(wc -l <"$WORK/migrations.txt" | tr -d ' ')
evidence_file_count=$(find "$WORK" -maxdepth 1 -type f | wc -l | tr -d ' ')
EOF

# Cleanup is explicit, UUID-scoped, and verified before PASS. Physical removal
# here is acceptance-fixture cleanup by the operator, not product governance.
cleanup_database
cleanup_object_files "$WS1"
cleanup_object_files "$WS2"
cleanup_database "$P3C_WS1" "$P3C_WS2" "$P3C_USER1" "$P3C_USER2"
cleanup_object_files "$P3C_WS1"
cleanup_object_files "$P3C_WS2"
assert_eq "$(psqlq "select count(*) from users where id in ('$USER1','$USER2')")" 0 "test users cleaned"
assert_eq "$(psqlq "select count(*) from workspaces where id in ('$WS1','$WS2')")" 0 "test workspaces cleaned"
assert_eq "$(object_file_count "$WS1")" 0 "workspace one object files cleaned"
assert_eq "$(object_file_count "$WS2")" 0 "workspace two object files cleaned"
assert_eq "$(psqlq "select count(*) from workspaces where id in ('$P3C_WS1','$P3C_WS2')")" 0 "phase3c retained workspaces cleaned"
assert_eq "$(psqlq "select count(*) from users where id in ('$P3C_USER1','$P3C_USER2')")" 0 "phase3c retained users cleaned"
assert_eq "$(object_file_count "$P3C_WS1")" 0 "phase3c workspace one objects cleaned"
assert_eq "$(object_file_count "$P3C_WS2")" 0 "phase3c workspace two objects cleaned"
assert_eq "$(psqlq "select count(*) from pg_trigger where not tgisinternal and tgname like 'phase3d_e2e_%'")" 0 "test triggers cleaned"
CLEANED=1
trap - EXIT

echo "PHASE3D_E2E_PASS"
echo "source_sha=$SOURCE_SHA"
echo "script_sha256=$SCRIPT_SHA"
echo "api_image=$API_IMAGE api_digest=$API_DIGEST"
echo "worker_image=$WORKER_IMAGE worker_digest=$WORKER_DIGEST"
echo "frontend_image=$FRONTEND_IMAGE frontend_digest=$FRONTEND_DIGEST"
echo "format_smoke=txt,csv,xls,xlsx xls_fixture_sha256=$XLS_FIXTURE_SHA"
echo "governance_physical_delete_count=0"
echo "migration_list=$WORK/migrations.txt"
echo "evidence_dir=$WORK"
echo "evidence_files=$(find "$WORK" -maxdepth 1 -type f -printf '%f\n' | sort | paste -sd, -)"
