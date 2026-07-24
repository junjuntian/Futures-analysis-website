begin;

create table if not exists users (
    id uuid primary key,
    username text not null,
    username_normalized text not null unique,
    password_hash text not null,
    password_params_version smallint not null,
    password_rehash_required boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    last_login_at timestamptz,
    disabled_at timestamptz,
    constraint users_username_normalized_not_blank check (length(username_normalized) > 0),
    constraint users_password_hash_not_blank check (length(password_hash) > 0)
);

create table if not exists workspaces (
    id uuid primary key,
    name text not null,
    owner_user_id uuid not null unique references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint workspaces_name_not_blank check (length(trim(name)) > 0)
);

create table if not exists workspace_memberships (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    user_id uuid not null unique references users(id) on delete restrict,
    role text not null default 'owner',
    created_at timestamptz not null default now(),
    constraint workspace_memberships_one_membership unique (workspace_id, user_id),
    constraint workspace_memberships_role check (role in ('owner'))
);

create table if not exists roles (
    name text primary key,
    description text not null
);

insert into roles (name, description)
values
    ('admin', 'System administration for identity control plane'),
    ('analyst', 'Future analytical workspace role'),
    ('viewer', 'Future read-only workspace role')
on conflict (name) do update set description = excluded.description;

create table if not exists user_roles (
    user_id uuid not null references users(id) on delete cascade,
    role_name text not null references roles(name) on delete restrict,
    created_at timestamptz not null default now(),
    primary key (user_id, role_name)
);

create table if not exists sessions (
    id uuid primary key,
    user_id uuid not null references users(id) on delete cascade,
    token_hash text not null unique,
    csrf_hash text,
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    absolute_expires_at timestamptz not null,
    idle_expires_at timestamptz not null,
    revoked_at timestamptz,
    revoke_reason text,
    rotated_from_session_id uuid references sessions(id) on delete set null,
    user_agent text,
    ip_address text,
    constraint sessions_token_hash_not_blank check (length(token_hash) > 0),
    constraint sessions_expiry_order check (absolute_expires_at >= created_at and idle_expires_at >= created_at)
);

create index if not exists sessions_user_active_idx
    on sessions (user_id, created_at)
    where revoked_at is null;

create index if not exists sessions_token_hash_idx
    on sessions (token_hash)
    where revoked_at is null;

create table if not exists security_events (
    id uuid primary key,
    actor_user_id uuid references users(id) on delete set null,
    event_type text not null,
    outcome text not null,
    request_id uuid not null,
    username_normalized text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint security_events_type_not_blank check (length(trim(event_type)) > 0),
    constraint security_events_outcome check (outcome in ('success', 'failure', 'denied'))
);

create index if not exists security_events_created_idx
    on security_events (created_at desc);

create table if not exists audit_logs (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    actor_user_id uuid references users(id) on delete set null,
    event_type text not null,
    outcome text not null,
    request_id uuid not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint audit_logs_event_type_not_blank check (length(trim(event_type)) > 0),
    constraint audit_logs_outcome check (outcome in ('success', 'failure', 'denied'))
);

alter table audit_logs enable row level security;
alter table audit_logs force row level security;

drop policy if exists audit_logs_workspace_isolation on audit_logs;
create policy audit_logs_workspace_isolation on audit_logs
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create table if not exists system_settings (
    key text primary key,
    value jsonb not null,
    locked_at timestamptz,
    updated_at timestamptz not null default now()
);

insert into system_settings (key, value)
values ('bootstrap', '{"completed": false}'::jsonb)
on conflict (key) do nothing;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'futures_runtime') then
        create role futures_runtime;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'futures_migrator') then
        create role futures_migrator;
    end if;
end $$;

grant usage on schema public to futures_runtime;
grant usage on schema app to futures_runtime;
grant connect on database futures_platform to futures_runtime;
grant execute on function app.current_workspace_id() to futures_runtime;
grant select, insert, update on users to futures_runtime;
grant select, insert, update on workspaces to futures_runtime;
grant select, insert, update on workspace_memberships to futures_runtime;
grant select on roles to futures_runtime;
grant select, insert on user_roles to futures_runtime;
grant select, insert, update on sessions to futures_runtime;
grant select, insert on security_events to futures_runtime;
grant select, insert on audit_logs to futures_runtime;
grant select, insert, update on system_settings to futures_runtime;
grant select on schema_versions to futures_runtime;

insert into schema_versions (version, description)
values ('202607240002', 'phase 2 identity workspace security')
on conflict (version) do nothing;

commit;
