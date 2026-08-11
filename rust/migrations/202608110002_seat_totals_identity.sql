-- 品种汇总行的身份要含得住 NULL 合约。
--
-- seat_history 的唯一约束里有 contract，而汇总行的 contract 是 NULL——普通 unique
-- 对 NULL 互不冲突，于是 upsert 的 on conflict 对汇总行**从不触发**，每次重灌都叠一层。
-- 生产实测已经叠出 3,120 行重复的郑商所官方汇总。页面没炸只是因为查询侧按来源去重
-- 顺手挡住了；任何直接 sum 这张表的人都会拿到翻倍的数。
--
-- 修法：先按身份去重（保留 id 最小的那行），再把约束重建为 unique nulls not distinct。
-- 幂等：约束已是 nulls-not-distinct（看它背后索引的 indnullsnotdistinct）就什么都不做，
-- 生产手工先行、部署重放都安全。

begin;

do $$
declare
    already_strict boolean;
    removed bigint;
begin
    select coalesce(bool_or(i.indnullsnotdistinct), false) into already_strict
      from pg_constraint c
      join pg_index i on i.indexrelid = c.conindid
     where c.conrelid = 'seat_history'::regclass
       and c.conname = 'seat_history_identity';

    if not already_strict then
        -- 迁移角色没有 BYPASSRLS，delete 会被 RLS 按 workspace 过滤——所以逐 workspace
        -- 设上下文再删，与 202608100006/202608100011 同一套路。
        declare target uuid;
        begin
            for target in select id from workspaces loop
                perform set_config('app.current_workspace_id', target::text, true);
                delete from seat_history s
                 using seat_history keep
                 where s.workspace_id = target
                   and keep.workspace_id = s.workspace_id
                   and keep.trade_date = s.trade_date
                   and keep.exchange = s.exchange
                   and keep.instrument = s.instrument
                   and keep.contract is not distinct from s.contract
                   and keep.is_variety_total = s.is_variety_total
                   and keep.rank_type = s.rank_type
                   and keep.member = s.member
                   and keep.source = s.source
                   and keep.id < s.id;
            end loop;
        end;

        alter table seat_history drop constraint seat_history_identity;
        alter table seat_history add constraint seat_history_identity
            unique nulls not distinct (
                workspace_id, trade_date, exchange, instrument, contract,
                is_variety_total, rank_type, member, source
            );
    end if;

    -- 自证：约束背后的索引必须是 nulls-not-distinct 的。
    select coalesce(bool_or(i.indnullsnotdistinct), false) into already_strict
      from pg_constraint c
      join pg_index i on i.indexrelid = c.conindid
     where c.conrelid = 'seat_history'::regclass
       and c.conname = 'seat_history_identity';
    if not already_strict then
        raise exception '汇总行的身份仍然含不住 NULL 合约，重灌会继续叠重复';
    end if;
end
$$;

insert into schema_versions (version, description)
values ('202608110002', 'Seat identity treats null contract as equal so totals cannot duplicate')
on conflict (version) do nothing;

commit;
