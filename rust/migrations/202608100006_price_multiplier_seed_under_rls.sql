begin;

-- `202608100003` added `price_multiplier` and seeded nothing.
--
-- Its comment claimed the migration role has BYPASSRLS. It does not:
-- `futures_migrator` is neither superuser nor bypassrls, and `instruments`
-- carries FORCE row level security keyed on `app.current_workspace_id()`. With
-- no workspace context set, the UPDATE matched zero rows.
--
-- The assertion written to catch exactly that silently agreed, because it read
-- through the same filter that had blocked the write: it counted the collected
-- instruments as 0 and the seeded ones as 0, found them equal, and passed. An
-- assertion that cannot see the rows it is asserting about is not an assertion.
--
-- So this repeats the seed the way `202608030002` already established -- one
-- workspace at a time, with the context set -- and asserts in a way that fails
-- when it can see nothing at all.

do $$
declare
    target_workspace_id uuid;
    present integer;
    seeded integer;
    total_seeded integer := 0;
begin
    for target_workspace_id in select id from workspaces order by id
    loop
        perform set_config('app.current_workspace_id', target_workspace_id::text, true);

        update instruments set price_multiplier = spec.multiplier,
                               updated_at = now()
          from (values
            ('JM', 60::numeric),   -- 60 吨/手, 元/吨,       0.5 元 -> 30 元/手
            ('JD', 10::numeric),   -- 5 吨/手,  元/500千克,  1 元   -> 10 元/手（报价单位不同）
            ('LH', 16::numeric),   -- 16 吨/手, 元/吨,       5 元   -> 80 元/手
            ('FG', 20::numeric),   -- 20 吨/手, 元/吨,       1 元   -> 20 元/手
            ('SA', 20::numeric),   -- 20 吨/手, 元/吨,       1 元   -> 20 元/手
            ('AP', 10::numeric),   -- 10 吨/手, 元/吨,       1 元   -> 10 元/手
            ('AU', 1000::numeric), -- 1000 克/手, 元/克,     0.02 元 -> 20 元/手
            ('AG', 15::numeric)    -- 15 千克/手, 元/千克,   1 元   -> 15 元/手
          ) as spec(code, multiplier)
         where instruments.workspace_id = target_workspace_id
           and upper(instruments.code) = spec.code
           and instruments.price_multiplier is distinct from spec.multiplier;

        select count(*) into present from instruments
         where workspace_id = target_workspace_id
           and upper(code) in ('JM','JD','LH','FG','SA','AP','AU','AG');
        select count(*) into seeded from instruments
         where workspace_id = target_workspace_id
           and upper(code) in ('JM','JD','LH','FG','SA','AP','AU','AG')
           and price_multiplier is not null;

        if seeded <> present then
            raise exception
                'workspace %: price multiplier reached % of % collected instruments',
                target_workspace_id, seeded, present;
        end if;

        -- 鸡蛋 is the reason this column exists: the contract is 5 tonnes but
        -- the price is quoted per 500kg, so a one-yuan move is worth 10 yuan a
        -- lot. Every other collected variety has the two coincide, which is
        -- what makes this one dangerous -- taken from the contract size it is
        -- out by exactly half, and the number looks entirely ordinary. Checked
        -- inside the loop so it is checked in every workspace, not whichever
        -- one the context happened to end on.
        if exists (
            select 1 from instruments
             where workspace_id = target_workspace_id
               and upper(code) = 'JD'
               and price_multiplier is distinct from 10
        ) then
            raise exception
                'workspace %: 鸡蛋 price_multiplier must be 10, not the 5 tonne contract size',
                target_workspace_id;
        end if;

        total_seeded := total_seeded + seeded;
    end loop;

    -- The part `202608100003` was missing. Zero seeded across every workspace
    -- means the rows were invisible, not that there was nothing to do, and that
    -- is the exact failure being repaired here.
    if total_seeded = 0 then
        raise exception
            'no instrument was reachable to seed; the migration role cannot see them';
    end if;
end $$;

insert into schema_versions (version, description)
values ('202608100006', 'Seed price_multiplier with the workspace context RLS requires')
on conflict (version) do nothing;

commit;
