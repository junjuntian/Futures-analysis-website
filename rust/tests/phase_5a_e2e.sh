#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

report_failure() {
  local status=$? line=$1
  echo "PHASE5A_E2E_FAIL line=$line status=$status" >&2
  exit "$status"
}
trap 'report_failure "$LINENO"' ERR

RELEASE_DIR=${PHASE5A_RELEASE_DIR:?set PHASE5A_RELEASE_DIR}
EVIDENCE_DIR=${PHASE5A_EVIDENCE_DIR:?set PHASE5A_EVIDENCE_DIR}
DB_SERVICE=${PHASE5A_DB_SERVICE:-postgres}
DB_USER=${PHASE5A_DB_USER:-futures_app}
DB_NAME=${PHASE5A_DB_NAME:-futures_platform}
BASE=${PHASE5A_BASE_URL:-http://127.0.0.1:8088}
ORIGIN=${PHASE5A_ORIGIN:-http://localhost:8088}
COOKIE_NAME=${PHASE5A_COOKIE_NAME:-futures_session}
SESSION_ONE=
SESSION_TWO=
FAVORITE_ID=
TEMP_USER_ID=
TEMP_WORKSPACE_ID=
TEMP_MEMBERSHIP_ID=

case "$RELEASE_DIR" in
  /opt/futures-platform-releases/*) ;;
  *) echo "PHASE5A_E2E_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac
install -d -m 700 "$EVIDENCE_DIR"

COMPOSE=(
  docker compose
  -f "$RELEASE_DIR/docker-compose.yml"
  -f "$RELEASE_DIR/docker-compose.production.yml"
  -f "$RELEASE_DIR/docker-compose.release.yml"
)

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
    echo "PHASE5A_E2E_FAIL http_label=$3 expected=$2 actual=$1" >&2
    exit 1
  }
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
urlencode() { jq -rn --arg value "$1" '$value|@uri'; }

cleanup() {
  local status=$?
  set +e
  if [[ "$FAVORITE_ID" =~ ^[0-9a-f-]{36}$ ]]; then
    api_json "$TOKEN_ONE" "$CSRF_ONE" DELETE \
      "/api/v1/spread-analytics/favorites/$FAVORITE_ID" '{}' \
      "$EVIDENCE_DIR/favorite-cleanup.json" >/dev/null 2>&1
  fi
  if [[ "$SESSION_ONE" =~ ^[0-9a-f-]{36}$ ]] && [[ "$SESSION_TWO" =~ ^[0-9a-f-]{36}$ ]]; then
    psql_value -c "delete from sessions where id in ('$SESSION_ONE','$SESSION_TWO')" \
      >/dev/null 2>&1
  fi
  if [[ "$TEMP_USER_ID" =~ ^[0-9a-f-]{36}$ ]] &&
     [[ "$TEMP_WORKSPACE_ID" =~ ^[0-9a-f-]{36}$ ]] &&
     [[ "$TEMP_MEMBERSHIP_ID" =~ ^[0-9a-f-]{36}$ ]]; then
    psql_value -c "delete from user_roles where user_id='$TEMP_USER_ID';
      delete from workspace_memberships where id='$TEMP_MEMBERSHIP_ID';
      delete from workspaces where id='$TEMP_WORKSPACE_ID';
      delete from users where id='$TEMP_USER_ID'" >/dev/null 2>&1
  fi
  exit "$status"
}
trap cleanup EXIT

echo "PHASE5A_E2E_STAGE preconditions"
test "$(psql_value -c "select count(*) from schema_versions where version='202608050001'")" = 1
test "$(psql_value -c "select count(*) from pg_class where relname in ('spread_provider_cache','spread_provider_throttles','spread_provider_failures','retail_trade_window_rule_versions','retail_trade_window_rules','spread_provider_series','spread_provider_observations','spread_window_segments','spread_favorites') and relkind='r'")" = 9
test "$(psql_value -c "select count(*) from pg_class where relname in ('spread_provider_series','spread_provider_observations','spread_window_segments','spread_favorites') and relrowsecurity and relforcerowsecurity")" = 4
test "$(psql_value -c "select count(*) from pg_policies where tablename in ('spread_provider_series','spread_provider_observations','spread_window_segments','spread_favorites')")" = 4
test "$(psql_value -c "select count(*) from retail_trade_window_rule_versions where version='retail-window-default-v1' and algorithm_version='retail_window_v1' and status='active'")" = 1
test "$(psql_value -c "select count(*) from pg_constraint where conname='spread_provider_cache_endpoint_allowed' and pg_get_constraintdef(oid) like '%all_varieties%' and pg_get_constraintdef(oid) like '%variety_contracts%' and pg_get_constraintdef(oid) like '%arbitrage_varieties%'")" = 1

backfill_state_before=$(systemctl show futures-backfill-phase4b1.service \
  -p ActiveState -p SubState -p Result --value --no-pager 2>/dev/null | tr '\n' '/' || true)
backfill_driver_before=$(/usr/local/sbin/run-futures-backfill --status 2>/dev/null |
  awk -F= '$1=="driver_running"{print $2}' || true)
test "$backfill_driver_before" = no
test "$(systemctl is-active futures-backfill-phase4b1.service 2>/dev/null || true)" = inactive

curl -fsS "$BASE/api-docs/openapi.json" >"$EVIDENCE_DIR/openapi.json"
jq -e '
  .paths["/api/v1/spread-analytics/providers/sanhe/varieties"].get and
  .paths["/api/v1/spread-analytics/providers/sanhe/varieties/{variety}/months"].get and
  .paths["/api/v1/spread-analytics/free-spread/query"].post and
  .paths["/api/v1/spread-analytics/favorites"].get and
  .paths["/api/v1/spread-analytics/favorites"].post and
  .paths["/api/v1/spread-analytics/favorites/{favorite_id}"].delete
' "$EVIDENCE_DIR/openapi.json" >/dev/null
frontend_cid=$("${COMPOSE[@]}" ps -q frontend | head -n1)
test -n "$frontend_cid"
test "$(docker exec "$frontend_cid" sh -lc "grep -R -l 'sanheshuju\\.com' /usr/share/nginx/html 2>/dev/null | wc -l")" = 0
echo "PHASE5A_E2E_STAGE preconditions_passed"

mapfile -t workspace_rows < <(psql_value -c \
  "select w.id::text || '|' || w.owner_user_id::text
     from workspaces w
    where exists (select 1 from user_roles r where r.user_id=w.owner_user_id and r.role_name='admin')
    order by (select count(*) from contracts c where c.workspace_id=w.id) desc,
             w.created_at, w.id
    limit 2")
if test "${#workspace_rows[@]}" -eq 1; then
  TEMP_USER_ID=$(new_uuid)
  TEMP_WORKSPACE_ID=$(new_uuid)
  TEMP_MEMBERSHIP_ID=$(new_uuid)
  temp_username="phase5a-e2e-$TEMP_USER_ID"
  psql_value -c "insert into users
      (id,username,username_normalized,password_hash,password_params_version)
    values
      ('$TEMP_USER_ID','$temp_username','$temp_username','phase5a-e2e-no-login',1);
    insert into workspaces (id,name,owner_user_id)
    values ('$TEMP_WORKSPACE_ID','Phase 5A VPS E2E','$TEMP_USER_ID');
    insert into workspace_memberships (id,workspace_id,user_id,role)
    values ('$TEMP_MEMBERSHIP_ID','$TEMP_WORKSPACE_ID','$TEMP_USER_ID','owner');
    insert into user_roles (user_id,role_name)
    values ('$TEMP_USER_ID','analyst')" >/dev/null
  workspace_rows+=("$TEMP_WORKSPACE_ID|$TEMP_USER_ID")
fi
test "${#workspace_rows[@]}" -eq 2
IFS='|' read -r WORKSPACE_ONE USER_ONE <<<"${workspace_rows[0]}"
IFS='|' read -r WORKSPACE_TWO USER_TWO <<<"${workspace_rows[1]}"
test "$WORKSPACE_ONE" != "$WORKSPACE_TWO"

SESSION_ONE=$(new_uuid)
SESSION_TWO=$(new_uuid)
TOKEN_ONE=$(openssl rand -hex 32)
TOKEN_TWO=$(openssl rand -hex 32)
CSRF_ONE=$(openssl rand -hex 32)
CSRF_TWO=$(openssl rand -hex 32)
token_one_hash=$(hash_token "$TOKEN_ONE")
token_two_hash=$(hash_token "$TOKEN_TWO")
csrf_one_hash=$(hash_token "$CSRF_ONE")
csrf_two_hash=$(hash_token "$CSRF_TWO")
psql_value -c "insert into sessions
  (id,user_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at,user_agent)
 values
  ('$SESSION_ONE','$USER_ONE','$token_one_hash','$csrf_one_hash',now()+interval '2 hours',now()+interval '2 hours','phase5a-vps-e2e'),
  ('$SESSION_TWO','$USER_TWO','$token_two_hash','$csrf_two_hash',now()+interval '2 hours',now()+interval '2 hours','phase5a-vps-e2e')" >/dev/null
unset token_one_hash token_two_hash csrf_one_hash csrf_two_hash

assert_status "$(curl -sS -o "$EVIDENCE_DIR/unauthorized.json" -w '%{http_code}' \
  "$BASE/api/v1/spread-analytics/favorites")" 401 "unauthorized favorites"
AUTH_PROBE_BODY='{"provider":"sanhe","leg1":{"variety":"probe-a","symbol":"PA","month":"01"},"leg2":{"variety":"probe-b","symbol":"PB","month":"02"}}'
assert_status "$(curl -sS -o "$EVIDENCE_DIR/query-unauthenticated.json" -w '%{http_code}' -X POST \
  -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  --data "$AUTH_PROBE_BODY" \
  "$BASE/api/v1/spread-analytics/free-spread/query")" 401 "query unauthenticated"
jq -e '.data.code == "auth_required"' "$EVIDENCE_DIR/query-unauthenticated.json" >/dev/null
assert_status "$(curl -sS -o "$EVIDENCE_DIR/csrf.json" -w '%{http_code}' -X POST \
  -H "Cookie: $COOKIE_NAME=$TOKEN_ONE" -H "Origin: $ORIGIN" \
  -H 'Content-Type: application/json' --data "$AUTH_PROBE_BODY" \
  "$BASE/api/v1/spread-analytics/free-spread/query")" 403 "query csrf"
jq -e '.data.code == "csrf_required"' "$EVIDENCE_DIR/csrf.json" >/dev/null
unset AUTH_PROBE_BODY

echo "PHASE5A_E2E_STAGE live_provider"
varieties_json="$EVIDENCE_DIR/varieties.json"
assert_status "$(api_get "$TOKEN_ONE" \
  '/api/v1/spread-analytics/providers/sanhe/varieties' "$varieties_json")" 200 "varieties"
jq -e '
  .data.source.provider == "sanhe" and
  .data.source.source_code == "sanhe_spread_readonly" and
  .data.source.source_display_name == "三禾数据" and
  .data.source.price_basis == "upstream_spread" and
  .data.source.raw_leg_prices_available == false and
  (.data.source.fetched_at | type == "string") and
  (.data.items | length >= 2)
' "$varieties_json" >/dev/null

# A cache hit must be a pure read: no upstream/throttle activity, and the API must
# replay the persisted payload plus source metadata byte-for-byte.
varieties_cache_count_before=$(psql_value -c "select count(*) from spread_provider_cache")
varieties_throttle_before=$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")
assert_status "$(api_get "$TOKEN_ONE" \
  '/api/v1/spread-analytics/providers/sanhe/varieties' "$EVIDENCE_DIR/varieties-hit.json")" 200 "varieties cache hit"
test "$(psql_value -c "select count(*) from spread_provider_cache")" = "$varieties_cache_count_before"
test "$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")" = "$varieties_throttle_before"
diff -u <(jq -S '.data' "$varieties_json") <(jq -S '.data' "$EVIDENCE_DIR/varieties-hit.json")

VARIETY_A=$(jq -r '.data.items[0].name' "$varieties_json")
VARIETY_B=$(jq -r '.data.items[1].name' "$varieties_json")
test "$VARIETY_A" != "$VARIETY_B"
PATH_A="/api/v1/spread-analytics/providers/sanhe/varieties/$(urlencode "$VARIETY_A")/months"
PATH_B="/api/v1/spread-analytics/providers/sanhe/varieties/$(urlencode "$VARIETY_B")/months"
throttle_before=$(psql_value -c \
  "select coalesce(floor(extract(epoch from last_requested_at)*1000)::bigint,0) from spread_provider_throttles where provider_code='sanhe'")
months_cache_before=$(psql_value -c \
  "select count(*) from spread_provider_cache where endpoint_code='variety_contracts' and business_date=(now() at time zone 'Asia/Shanghai')::date")
(
  status=$(api_get "$TOKEN_ONE" "$PATH_A" "$EVIDENCE_DIR/months-a.json")
  assert_status "$status" 200 "months a"
) &
pid_a=$!
(
  status=$(api_get "$TOKEN_ONE" "$PATH_B" "$EVIDENCE_DIR/months-b.json")
  assert_status "$status" 200 "months b"
) &
pid_b=$!
wait "$pid_a"
wait "$pid_b"
throttle_after=$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")
throttle_delta_ms=$((throttle_after - throttle_before))
months_cache_after=$(psql_value -c \
  "select count(*) from spread_provider_cache where endpoint_code='variety_contracts' and business_date=(now() at time zone 'Asia/Shanghai')::date")
# Spacing is only observable when both probes actually reached upstream. A
# same-day redeploy finds these two varieties already cached, so no request is
# issued and the throttle clock does not move; that is the cache working, not a
# regression. Assert spacing only for the requests that did go out.
upstream_requests=$((months_cache_after - months_cache_before))
if test "$upstream_requests" -ge 2; then
  test "$throttle_delta_ms" -ge 4000
elif test "$upstream_requests" -eq 1; then
  test "$throttle_delta_ms" -ge 2000
else
  test "$throttle_delta_ms" -eq 0
fi
echo "PHASE5A_THROTTLE upstream_requests=$upstream_requests delta_ms=$throttle_delta_ms"
jq -e '.data.result_kind == "ok" and (.data.months | type == "array")' \
  "$EVIDENCE_DIR/months-a.json" "$EVIDENCE_DIR/months-b.json" >/dev/null

cache_count_before=$(psql_value -c "select count(*) from spread_provider_cache")
cache_fetched_before=$(jq -r '.data.source.fetched_at' "$EVIDENCE_DIR/months-a.json")
throttle_hit_before=$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")
assert_status "$(api_get "$TOKEN_ONE" "$PATH_A" "$EVIDENCE_DIR/months-a-hit.json")" 200 "months cache hit"
cache_count_after=$(psql_value -c "select count(*) from spread_provider_cache")
cache_fetched_after=$(jq -r '.data.source.fetched_at' "$EVIDENCE_DIR/months-a-hit.json")
throttle_hit_after=$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")
test "$cache_count_after" = "$cache_count_before"
test "$cache_fetched_after" = "$cache_fetched_before"
test "$throttle_hit_after" = "$throttle_hit_before"
diff -u <(jq -S '.data' "$EVIDENCE_DIR/months-a.json") \
  <(jq -S '.data' "$EVIDENCE_DIR/months-a-hit.json")

JM_NAME=$(jq -r '.data.items[] | select((.symbol|ascii_upcase)=="JM") | .name' \
  "$varieties_json" | head -n1)
test -n "$JM_NAME"
JM_SYMBOL=$(jq -r --arg name "$JM_NAME" '.data.items[] | select(.name==$name) | .symbol' \
  "$varieties_json" | head -n1)
JM_PATH="/api/v1/spread-analytics/providers/sanhe/varieties/$(urlencode "$JM_NAME")/months"
assert_status "$(api_get "$TOKEN_ONE" "$JM_PATH" "$EVIDENCE_DIR/jm-months.json")" 200 "jm months"
jq -e '.data.months | index("09") and index("01")' "$EVIDENCE_DIR/jm-months.json" >/dev/null

query_body=$(jq -cn --arg variety "$JM_NAME" --arg symbol "$JM_SYMBOL" '
  {provider:"sanhe",leg1:{variety:$variety,symbol:$symbol,month:"09"},leg2:{variety:$variety,symbol:$symbol,month:"01"}}')
query_json="$EVIDENCE_DIR/jm-09-01.json"
assert_status "$(api_json "$TOKEN_ONE" "$CSRF_ONE" POST \
  '/api/v1/spread-analytics/free-spread/query' "$query_body" "$query_json")" 200 "jm 09-01"
jq -e '
  .data.source.provider == "sanhe" and
  .data.source.source_code == "sanhe_spread_readonly" and
  .data.source.source_display_name == "三禾数据" and
  .data.source.price_basis == "upstream_spread" and
  .data.source.raw_leg_prices_available == false and
  (.data.source.fetched_at | type == "string") and
  .data.algorithm_versions.provider == "sanhe_spread_v1" and
  .data.algorithm_versions.window == "retail_window_v1" and
  .data.algorithm_versions.statistics == "spread_window_stats_v1" and
  .data.algorithm_versions.rule == "retail-window-default-v1" and
  (.data.quality.input_point_count >= .data.quality.retained_point_count) and
  (.data.continuous_series.points | length) == .data.quality.retained_point_count and
  (.data.segments | length) >= 1 and
  (.data.seasonal_series.axis | type == "array") and
  (.data.monthly_matrix.up_ratios | length) == 12
' "$query_json" >/dev/null
series_id=$(jq -r '.data.series_id' "$query_json")
test "$(psql_value -c "select count(*) from spread_provider_series where id='$series_id' and workspace_id='$WORKSPACE_ONE' and price_basis='upstream_spread'")" = 1
test "$(psql_value -c "select count(*) from spread_provider_observations where series_id='$series_id' and workspace_id='$WORKSPACE_ONE'")" = "$(jq -r '.data.quality.input_point_count' "$query_json")"

query_cache_count_before=$(psql_value -c "select count(*) from spread_provider_cache")
query_throttle_before=$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")
query_hit_json="$EVIDENCE_DIR/jm-09-01-hit.json"
assert_status "$(api_json "$TOKEN_ONE" "$CSRF_ONE" POST \
  '/api/v1/spread-analytics/free-spread/query' "$query_body" "$query_hit_json")" 200 "jm 09-01 cache hit"
test "$(psql_value -c "select count(*) from spread_provider_cache")" = "$query_cache_count_before"
test "$(psql_value -c \
  "select floor(extract(epoch from last_requested_at)*1000)::bigint from spread_provider_throttles where provider_code='sanhe'")" = "$query_throttle_before"
diff -u <(jq -S '.data | del(.series_id)' "$query_json") \
  <(jq -S '.data | del(.series_id)' "$query_hit_json")
echo "PHASE5A_E2E_STAGE live_provider_passed"

echo "PHASE5A_E2E_STAGE favorites_and_rls"
favorite_body=$(jq -cn --arg variety "$JM_NAME" --arg symbol "$JM_SYMBOL" '
  {name:"Phase 5A VPS E2E",provider:"sanhe",leg1:{variety:$variety,symbol:$symbol,month:"09"},leg2:{variety:$variety,symbol:$symbol,month:"01"}}')
assert_status "$(api_json "$TOKEN_ONE" "$CSRF_ONE" POST \
  '/api/v1/spread-analytics/favorites' "$favorite_body" "$EVIDENCE_DIR/favorite-create.json")" 201 "favorite create"
FAVORITE_ID=$(jq -r '.data.id' "$EVIDENCE_DIR/favorite-create.json")
test "$(api_json "$TOKEN_TWO" "$CSRF_TWO" DELETE \
  "/api/v1/spread-analytics/favorites/$FAVORITE_ID" '{}' "$EVIDENCE_DIR/favorite-cross-delete.json")" = 404
assert_status "$(api_get "$TOKEN_TWO" '/api/v1/spread-analytics/favorites' \
  "$EVIDENCE_DIR/favorites-workspace-two.json")" 200 "workspace two favorites"
jq -e --arg id "$FAVORITE_ID" '[.data[] | select(.id==$id)] | length == 0' \
  "$EVIDENCE_DIR/favorites-workspace-two.json" >/dev/null
rls_cross_count=$(psql_value -c \
  "begin; set local role futures_runtime; select set_config('app.workspace_id','$WORKSPACE_TWO',true); select count(*) from spread_favorites where id='$FAVORITE_ID'; rollback" | tail -n1)
test "$rls_cross_count" = 0
assert_status "$(api_json "$TOKEN_ONE" "$CSRF_ONE" DELETE \
  "/api/v1/spread-analytics/favorites/$FAVORITE_ID" '{}' "$EVIDENCE_DIR/favorite-delete.json")" 204 "favorite delete"
FAVORITE_ID=
test "$(psql_value -c "select count(*) from audit_logs where event_type in ('spread.favorite.created','spread.favorite.deleted') and workspace_id='$WORKSPACE_ONE'")" -ge 2
echo "PHASE5A_E2E_STAGE favorites_and_rls_passed"

backfill_state_after=$(systemctl show futures-backfill-phase4b1.service \
  -p ActiveState -p SubState -p Result --value --no-pager 2>/dev/null | tr '\n' '/' || true)
backfill_driver_after=$(/usr/local/sbin/run-futures-backfill --status 2>/dev/null |
  awk -F= '$1=="driver_running"{print $2}' || true)
test "$backfill_state_after" = "$backfill_state_before"
test "$backfill_driver_after" = "$backfill_driver_before"

input_points=$(jq -r '.data.quality.input_point_count' "$query_json")
retained_points=$(jq -r '.data.quality.retained_point_count' "$query_json")
excluded_points=$(jq -r '.data.quality.excluded_point_count' "$query_json")
segment_count=$(jq -r '.data.segments | length' "$query_json")
fetched_at=$(jq -r '.data.source.fetched_at' "$query_json")
source_code=$(jq -r '.data.source.source_code' "$query_json")
printf 'PHASE5A_REAL_QUERY combination=JM-09-01 input_points=%s retained_points=%s excluded_points=%s segments=%s source=%s fetched_at=%s throttle_concurrent_delta_ms=%s\n' \
  "$input_points" "$retained_points" "$excluded_points" "$segment_count" \
  "$source_code" "$fetched_at" "$throttle_delta_ms"
echo PHASE5A_E2E_PASS
