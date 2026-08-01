begin;

create or replace function app.prevent_import_mapping_template_rebind()
returns trigger
language plpgsql
as $$
begin
    if old.template_version_id is not null
       and new.template_version_id is distinct from old.template_version_id then
        raise exception 'template_version_id cannot be rebound once set'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create trigger import_mappings_prevent_template_rebind
before update of template_version_id on import_mappings
for each row
execute function app.prevent_import_mapping_template_rebind();

insert into schema_versions (version, description)
values ('202607250004', 'phase 3b preview invalidation and template binding guard')
on conflict (version) do nothing;

commit;
