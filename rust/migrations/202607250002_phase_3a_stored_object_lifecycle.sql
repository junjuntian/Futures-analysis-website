begin;

alter table stored_objects
    rename column content_type to mime_type;

alter table stored_objects
    add column backend text not null default 'local',
    add column state text not null default 'available',
    add column retention_until timestamptz;

alter table stored_objects
    alter column backend drop default,
    alter column state drop default,
    add constraint stored_objects_backend
        check (backend in ('local', 's3')),
    add constraint stored_objects_state
        check (state in ('pending', 'available', 'deleting', 'deleted'));

create index stored_objects_retention_idx
    on stored_objects (workspace_id, retention_until)
    where retention_until is not null and state = 'available';

insert into schema_versions (version, description)
values ('202607250002', 'phase 3a stored object lifecycle correction')
on conflict (version) do nothing;

commit;
