begin;

-- Segments whose legs expire in the wrong order (the upstream splices the
-- reverse combination, e.g. jm2609-jm2601 inside a 09-01 query) are excluded
-- from the retail window. Allow the new exclusion and boundary reason.
alter table spread_provider_observations
    drop constraint spread_provider_observations_exclusion_allowed;

alter table spread_provider_observations
    add constraint spread_provider_observations_exclusion_allowed check (
        exclusion_reason is null or exclusion_reason in (
            'contract_metadata_missing', 'outside_retail_window',
            'empty_retail_window', 'leg_order_mismatch'
        )
    );

alter table spread_window_segments
    drop constraint spread_window_segments_boundary_reason;

alter table spread_window_segments
    add constraint spread_window_segments_boundary_reason check (
        boundary_reason in (
            'retail_deadline', 'contract_metadata_missing',
            'empty_retail_window', 'leg_order_mismatch'
        )
    );

insert into schema_versions (version, description)
values ('202608090001', 'Phase 5A leg order mismatch exclusion and boundary reason')
on conflict (version) do nothing;

commit;
