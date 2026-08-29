-- 铁矿石 I 的点值(2026-08-28,运营者要求把铁矿石加进品种列表)。
--
-- 100 吨/手,报价 元/吨,最小变动 0.5 元/吨 → 一手最小波动 50 元。
-- 口径与既有八品种同(见 202608100003):点值 = 一手合约最小波动 ÷ 最小变动价位。
--
-- **只 update 不 insert**:instruments 行由每日 catalog 采集创建
-- (deploy/collector/load-catalog-direct.sql),那里带血缘与交易所外键;
-- 迁移凭空插行既缺 source_record 也缺 exchange_id,是错的。
-- **不断言行数**:铁矿石可能要等下一次 catalog 采集才第一次出现,那时这条迁移
-- 早跑完了 —— 所以 load-catalog-direct.sql 末尾有一段同规格的兜底补写,
-- 两处配合才保证「无论谁先到,点值都会补上」。改点值要同时改那两处。
update instruments set price_multiplier = 100, updated_at = now()
 where upper(code) = 'I' and (price_multiplier is null or price_multiplier <> 100);
