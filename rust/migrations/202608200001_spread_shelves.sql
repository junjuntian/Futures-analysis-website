-- 套利监控:价差自己的**平台位**(横盘转折位)与日波动。DEC-095。
--
-- 来历:运营者 2026-08-19 指出「最有利 +1360」这类历年点数**跨起点搬不动**——
-- 那 5 个历史实例的起点是 −815/+90/+685/+2645/+3305,而今天是 −935,在分布之外。
-- 他实际下单看的是另一样东西:**这一对合约自己图上的横盘位**。原话是
-- 「上一轮 −1700 到 −1355 就上不去了,−1355 就是向上的平台位;收盘突破平台位,
-- 才能继续往下看」。页面此前完全没有这一维。
--
-- 口径(参数是拿运营者点名的 −1355 试出来的,不是拍的):
--   · 转折位 = 收盘价差是**前后各 3 个交易日**里的最高或最低。
--     ±5 与 ±7 会把 −1355 弄丢,±2 会把 −1150/−1155/−1160/−1170 拆成四档。
--   · 50 点以内的相邻转折位并成一档,取均值。
--   · 触碰回合 = 收盘落在该档 ±25 点内的**独立回合数**(连续日算一回合)。
--   · 序列用**完整价差历史**,不是监控窗口那一段——LH2611−LH2705 在监控表里
--     只有两个月,只能识别出 3 档,而完整历史有 8 档。
--
-- 存的是事实(档位、触碰次数、日波动),**不存结论**:距离、到达概率、哪一档是
-- 卖点/止损,全部读时算(与报警/拐头/合格同一条纪律)。
-- 幂等:同 0001/0003/0004/0005/0006 的教训。

begin;

alter table spread_monitor_daily
    -- [{"level": -1355, "touches": 3}, ...],按档位从高到低
    add column if not exists shelves jsonb,
    -- 近 20 个交易日价差日变动的样本标准差,读时算「距离 ÷ σ√剩余天数」用
    add column if not exists spread_sigma numeric;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_shelves_sane,
    add constraint spread_monitor_daily_shelves_sane
        check ((shelves is null or jsonb_typeof(shelves) = 'array')
           and (spread_sigma is null or spread_sigma > 0));

insert into schema_versions (version, description)
values ('202608200001',
        'Spread monitor: swing-pivot shelf levels with touch counts, plus rolling spread volatility')
on conflict (version) do nothing;

commit;
