begin;

create sequence worker_dispatch_ticket_seq
    as bigint
    minvalue 1
    start with 1
    increment by 1
    no cycle;

alter table workspaces
    add column import_job_last_served_ticket bigint not null default 0,
    add column object_job_last_served_ticket bigint not null default 0,
    add constraint workspaces_import_job_ticket_nonnegative
        check (import_job_last_served_ticket >= 0),
    add constraint workspaces_object_job_ticket_nonnegative
        check (object_job_last_served_ticket >= 0);

grant usage, select on sequence worker_dispatch_ticket_seq to futures_runtime;
grant update (import_job_last_served_ticket, object_job_last_served_ticket)
    on workspaces to futures_runtime;

insert into schema_versions (version, description)
values ('202607260002', 'phase 3d persistent fair worker dispatch')
on conflict (version) do nothing;

commit;
