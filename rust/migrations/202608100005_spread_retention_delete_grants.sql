begin;

-- The retention added with the stored spread history deletes rows; the grants
-- never followed. `futures_runtime` -- the role the API actually connects as --
-- held only select/insert on the series and its children, and select/insert/
-- update on the cache, so:
--
--   * trimming the provider cache logged `permission denied for table
--     spread_provider_cache` on every upstream fetch, and
--   * bounding the stored series failed inside the transaction that had just
--     written it, which is the 500 that failed the Phase 5A acceptance on
--     `free-spread/query` for 焦煤 09-01 and rolled back two releases.
--
-- Both were invisible in testing because `futures_app` is a superuser: every
-- privilege check passes for it, so a grant that was never written looks
-- exactly like one that was. Checking the wrong role is what cost the time
-- here -- the evidence was `permission denied` in the failure log all along.
--
-- The observations and window segments are reached by `on delete cascade` from
-- the series. A cascade is still a delete on the referencing table and needs
-- the privilege there too, so granting it on the parent alone would leave the
-- same error one table further down.

grant delete on spread_provider_cache to futures_runtime;
grant delete on spread_provider_series to futures_runtime;
grant delete on spread_provider_observations to futures_runtime;
grant delete on spread_window_segments to futures_runtime;

-- Prove the grants landed rather than trusting that the statements above ran:
-- a migration that silently no-ops leaves exactly the failure it was written to
-- fix, and the next sign of it would be another rolled-back release.
do $$
declare
    missing text;
begin
    select string_agg(t.name, ', ' order by t.name)
      into missing
      from (values
              ('spread_provider_cache'),
              ('spread_provider_series'),
              ('spread_provider_observations'),
              ('spread_window_segments')
           ) as t(name)
     where not has_table_privilege('futures_runtime', t.name, 'DELETE');
    if missing is not null then
        raise exception 'futures_runtime still cannot delete from: %', missing;
    end if;
end
$$;

insert into schema_versions (version, description)
values ('202608100005', 'Delete grants for spread retention on the runtime role')
on conflict (version) do nothing;

commit;
