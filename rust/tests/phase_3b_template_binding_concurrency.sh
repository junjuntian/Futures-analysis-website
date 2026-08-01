#!/usr/bin/env bash
# Run after migrations 202607250004 through 202607250007.
# It creates only the fixed phase3b-concurrency-test UUIDs below and removes them on exit.
set -euo pipefail

# Default mode: DATABASE_URL is passed to local psql.
# Docker mode example (no host psql required):
#   PSQL_WRAPPER='docker compose --profile dev exec -T postgres psql -U futures_migrator -d futures_platform' \
#   PSQL_CONNECT_ARGS='' ./phase_3b_template_binding_concurrency.sh
: "${PSQL_WRAPPER:=psql}"
: "${PSQL_CONNECT_ARGS:=${DATABASE_URL:-}}"
if [[ "$PSQL_WRAPPER" == 'psql' && -z "$PSQL_CONNECT_ARGS" ]]; then
  echo 'set DATABASE_URL or PSQL_WRAPPER with its PostgreSQL connection arguments' >&2
  exit 2
fi
read -r -a psql_wrapper <<< "$PSQL_WRAPPER"
read -r -a psql_connect_args <<< "$PSQL_CONNECT_ARGS"

run_psql() {
  "${psql_wrapper[@]}" "${psql_connect_args[@]}" "$@"
}

cleanup() {
  run_psql -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
delete from import_mappings where id = '31000000-0000-7000-8000-000000000051';
delete from import_batches where id = '31000000-0000-7000-8000-000000000041';
delete from import_template_versions where id in ('31000000-0000-7000-8000-000000000031', '31000000-0000-7000-8000-000000000032', '31000000-0000-7000-8000-000000000033');
delete from import_templates where id in ('31000000-0000-7000-8000-000000000021', '31000000-0000-7000-8000-000000000022', '31000000-0000-7000-8000-000000000023');
delete from workspaces where id = '31000000-0000-7000-8000-000000000011';
delete from users where id = '31000000-0000-7000-8000-000000000001';
SQL
}
trap cleanup EXIT
cleanup

run_psql -v ON_ERROR_STOP=1 -q <<'SQL'
insert into users (id, username, username_normalized, password_hash, password_params_version)
values ('31000000-0000-7000-8000-000000000001', 'phase3b-concurrency', 'phase3b-concurrency', 'test', 1);
insert into workspaces (id, name, owner_user_id)
values ('31000000-0000-7000-8000-000000000011', 'phase3b-concurrency', '31000000-0000-7000-8000-000000000001');
begin;
set local app.current_workspace_id = '31000000-0000-7000-8000-000000000011';
insert into import_templates (id, workspace_id, dataset_type, name, created_by)
values
  ('31000000-0000-7000-8000-000000000021', '31000000-0000-7000-8000-000000000011', 'generic', 'phase3b-concurrency-one', '31000000-0000-7000-8000-000000000001'),
  ('31000000-0000-7000-8000-000000000022', '31000000-0000-7000-8000-000000000011', 'generic', 'phase3b-concurrency-two', '31000000-0000-7000-8000-000000000001'),
  ('31000000-0000-7000-8000-000000000023', '31000000-0000-7000-8000-000000000011', 'generic', 'phase3b-freeze-race', '31000000-0000-7000-8000-000000000001');
insert into import_template_versions
  (id, workspace_id, template_id, version_number, dataset_type, configuration_json, created_by)
values
  ('31000000-0000-7000-8000-000000000031', '31000000-0000-7000-8000-000000000011', '31000000-0000-7000-8000-000000000021', 1, 'generic', '{"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]}'::jsonb, '31000000-0000-7000-8000-000000000001'),
  ('31000000-0000-7000-8000-000000000032', '31000000-0000-7000-8000-000000000011', '31000000-0000-7000-8000-000000000022', 1, 'generic', '{"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]}'::jsonb, '31000000-0000-7000-8000-000000000001');
insert into import_batches (id, workspace_id, status, dataset_type, created_by)
values ('31000000-0000-7000-8000-000000000041', '31000000-0000-7000-8000-000000000011', 'mapped', 'generic', '31000000-0000-7000-8000-000000000001');
commit;
SQL

run_psql -v ON_ERROR_STOP=1 -q <<'SQL' &
begin;
set local app.current_workspace_id = '31000000-0000-7000-8000-000000000011';
\o /dev/null
select id from import_batches where id = '31000000-0000-7000-8000-000000000041' for update;
\o
insert into import_mappings (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
values ('31000000-0000-7000-8000-000000000051', '31000000-0000-7000-8000-000000000011', '31000000-0000-7000-8000-000000000041', '31000000-0000-7000-8000-000000000031', 'generic', '{"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]}'::jsonb, '31000000-0000-7000-8000-000000000001');
select pg_sleep(1);
commit;
SQL
first_pid=$!
sleep 0.2

second_result="$(run_psql -v ON_ERROR_STOP=1 -q -tA <<'SQL'
begin;
set local app.current_workspace_id = '31000000-0000-7000-8000-000000000011';
\o /dev/null
select id from import_batches where id = '31000000-0000-7000-8000-000000000041' for update;
\o
insert into import_mappings (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
values ('31000000-0000-7000-8000-000000000052', '31000000-0000-7000-8000-000000000011', '31000000-0000-7000-8000-000000000041', '31000000-0000-7000-8000-000000000032', 'generic', '{"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]}'::jsonb, '31000000-0000-7000-8000-000000000001')
on conflict (workspace_id, import_batch_id) do update
set template_version_id = excluded.template_version_id,
    dataset_type = excluded.dataset_type,
    mapping_json = excluded.mapping_json,
    updated_at = now()
where import_mappings.template_version_id is null
   or import_mappings.template_version_id = excluded.template_version_id
returning id;
commit;
SQL
)"
wait "$first_pid"

if [[ -n "$second_result" ]]; then
  echo 'expected second concurrent template binding to affect zero rows' >&2
  exit 1
fi

bound_version="$(run_psql -tA -c "select template_version_id from import_mappings where id = '31000000-0000-7000-8000-000000000051'")"
[[ "$bound_version" == '31000000-0000-7000-8000-000000000031' ]]

run_psql -v ON_ERROR_STOP=1 -q <<'SQL' &
begin;
set local app.current_workspace_id = '31000000-0000-7000-8000-000000000011';
insert into import_template_versions
  (id, workspace_id, template_id, version_number, dataset_type, configuration_json, created_by)
values
  ('31000000-0000-7000-8000-000000000033', '31000000-0000-7000-8000-000000000011', '31000000-0000-7000-8000-000000000023', 1, 'generic', '{"fields":[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]}'::jsonb, '31000000-0000-7000-8000-000000000001');
select pg_sleep(1);
commit;
SQL
freeze_insert_pid=$!
sleep 0.2

if run_psql -v ON_ERROR_STOP=1 -q <<'SQL'
begin;
set local app.current_workspace_id = '31000000-0000-7000-8000-000000000011';
update import_templates
   set dataset_type = 'changed'
 where id = '31000000-0000-7000-8000-000000000023';
commit;
SQL
then
  echo 'expected concurrent parent dataset_type update to fail after version insertion' >&2
  exit 1
fi
wait "$freeze_insert_pid"

freeze_state="$(run_psql -tA -c "select t.dataset_type || '|' || v.dataset_type from import_templates t join import_template_versions v on v.workspace_id = t.workspace_id and v.template_id = t.id where t.id = '31000000-0000-7000-8000-000000000023'")"
[[ "$freeze_state" == 'generic|generic' ]]
echo 'PHASE3B_TEMPLATE_BINDING_CONCURRENCY_PASS'
