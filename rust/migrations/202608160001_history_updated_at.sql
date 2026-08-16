-- 采集源到达时刻画像(2026-08-16 运营者立项)拆出的第二个时间戳。
--
-- 一列扛不动两个语义:loaded_at 原来每次 upsert 都被推到 now(),E2E 靠它验
-- 「本轮真的写过」;而到达时刻画像要求它保持「首次入库时刻」不动。首版只改
-- 装载不加列,当晚部署即被 4A E2E 的静默 test 拦下回滚(count(loaded_at 新鲜
-- 行)=0)——两个语义正面相撞的实证。
--
-- 自此分工:loaded_at = 行首次入库时刻(装载不再刷新,min 聚合=数据源当日
-- 首次到达);updated_at = 最近一次装载触碰时刻(insert 与 upsert 都填 now(),
-- E2E 的「刚写过」断言改看它)。
--
-- 可空、无默认:加列瞬时完成,不触发全表重写(两表合计 600 万+行,volatile
-- default 会锁表重写)。存量行为 null = 2026-08-16 前最后触碰时刻不可考。
begin;

alter table seat_history add column if not exists updated_at timestamptz;
alter table price_history add column if not exists updated_at timestamptz;

comment on column seat_history.updated_at is
  '最近一次装载触碰时刻(insert/upsert 均刷新)。null=2026-08-16 加列前的存量行。';
comment on column price_history.updated_at is
  '最近一次装载触碰时刻(insert/upsert 均刷新)。null=2026-08-16 加列前的存量行。';

insert into schema_versions (version, description)
values ('202608160001', 'seat/price_history updated_at: last-touch timestamp so loaded_at can stay first-arrival')
on conflict (version) do nothing;

commit;
