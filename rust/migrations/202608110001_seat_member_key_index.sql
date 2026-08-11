-- 给「归一后的会员名」建索引。
--
-- 会员名跨数据源写法不一致（同一家在 akshare 下是「国泰君安」，在东财下是
-- 「国泰君安（代客）」），所以查询按 `regexp_replace(member, …)` 归一之后再比。
-- 但函数一套上去，原有的 (workspace_id, member, instrument, trade_date) 索引就废了：
-- 索引里存的是原文，查询要的是归一后的值，对不上。
--
-- 实测代价（生产，seat_history 380 万行）：
--   会员名录      2.5 秒（全表扫描，每行算一次正则）
--   某会员的交易日 3.7 秒（扫完 380 万行索引才挑出 2061 条）
--   当日持仓明细  28 毫秒（这条走索引，没受影响）
-- 也就是切一次席位要等六秒以上，而真正取数据只用 28 毫秒。
--
-- 表达式索引正是为这种情况准备的：把归一后的值也建进索引，查询就能重新走索引。
-- 代价是每次写入多算一次正则并多维护一棵树；这两张表是日更、每天几千行，
-- 换掉六秒的等待很划算。
--
-- 表达式必须与 `MEMBER_KEY`（crates/database/src/spread_analytics.rs）**逐字一致**，
-- 差一个字符索引就用不上，而且不会报错，只会悄悄变慢——那正是这条迁移要治的病。

begin;

-- if not exists:这棵索引先用 create index concurrently 在生产上建好并实测,
-- 才写进迁移(见上文实测数字)。迁移在生产重放时索引已在,撞名即整单失败——
-- 2026-08-11 部署 Run 31465309414 因此回滚,故必须幂等。
create index if not exists seat_history_by_member_key
    on seat_history (
        workspace_id,
        (regexp_replace(member, '[（(][^）)]*[）)]$', '')),
        instrument,
        trade_date
    );

-- 名录那条按归一后的会员分组、取每组最大持仓，上面那棵树能覆盖它的分组键。
-- 交易日那条按 (workspace_id, 归一名) 定位后直接拿 trade_date，也走同一棵。

do $$
begin
    if not exists (
        select 1 from pg_indexes
         where tablename = 'seat_history' and indexname = 'seat_history_by_member_key'
    ) then
        raise exception '表达式索引没建上，席位页会继续全表扫描';
    end if;
    -- 索引定义里必须真的含有那个正则，否则建的是另一棵树，查询照样用不上。
    if not exists (
        select 1 from pg_indexes
         where indexname = 'seat_history_by_member_key'
           and indexdef like '%regexp_replace%'
    ) then
        raise exception '索引不是建在归一表达式上的';
    end if;
end
$$;

insert into schema_versions (version, description)
values ('202608110001', 'Index the normalised seat member name the queries filter on')
on conflict (version) do nothing;

commit;
