-- Run with a privileged PostgreSQL role after migration 202607250008.
\set ON_ERROR_STOP on

do $$
begin
    if not has_table_privilege('futures_runtime', 'import_staging_rows', 'UPDATE') then
        raise exception 'futures_runtime must update import_staging_rows during full validation';
    end if;
    if not has_table_privilege('futures_runtime', 'import_staging_rows', 'SELECT, INSERT, DELETE') then
        raise exception 'futures_runtime import_staging_rows CRUD grant is incomplete';
    end if;
end;
$$;
