-- 总览页「数据到齐了吗」那条查询的专用索引。
--
-- 那条查询问的是:最近 45 个交易日,每天有哪几家交易所的数据到了。它按 trade_date
-- 分组、对 exchange 去重,回来的只有几十行——但为了得出这几十行,要把窗口内全部
-- 席位行读一遍再排序。2026-08-13 新机器上实测:扫 82,031 行、耗时 1.1 秒,日志
-- 直接报 slow statement(老机器多核时几十毫秒,所以此前没暴露)。
--
-- 现有索引都对不上这个形状:by_contract / by_member / by_instrument 的第二列分别是
-- 合约、会员、品种,而这条查询只按 workspace + 交易日筛;identity 那条虽以
-- (workspace_id, trade_date) 开头,却因为后面还有六列而体积庞大,也不含
-- is_variety_total 的过滤。
--
-- 这条索引三点针对性:
--   1. (workspace_id, trade_date desc, exchange) 与查询的分组和排序方向一致,
--      规划器可以顺着索引取,不必先全排一遍;
--   2. 只索引 not is_variety_total 的行——查询本来就只看这些,而汇总行占了
--      seat_history 约三成,把它们挡在索引外面;
--   3. 只有这三列,能走 index-only scan,不必回表——这是耗时的大头。
--
-- 不用 concurrently:那个写法不能放进事务,而本仓库的迁移契约要求每个文件带
-- begin/commit(断言失败时不留半应用的 schema),前置检查也在守这一条。普通建法
-- 在 604 万行上约一两分钟,期间持写锁——而部署流程本来就先拿了日更那把 flock,
-- 不会与采集撞上;失败还能整体回滚,不像 concurrently 会留下一个 invalid 索引
-- 等人手工清。
-- if not exists:生产上可能已有人手工建过同名索引(部署曾因此失败过一次)。

begin;

create index if not exists seat_history_by_date_exchange
    on seat_history (workspace_id, trade_date desc, exchange)
    where not is_variety_total;

insert into schema_versions (version, description)
values ('202608130001', 'seat_history 按交易日与交易所的部分索引,给总览页的数据到齐检查')
on conflict (version) do nothing;

commit;
