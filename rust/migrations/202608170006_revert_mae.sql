-- 套利监控:历年 MAE 两档(中位/最大),仓位预算的分母(DEC-067,因子②)。
--
-- 来历:运营者交来两份交易文档,《体系》模块四的盈亏比框架与《盖楼》猪 11-05 的
-- 「首笔/补仓/风险预留」分批法,数据化后就是:从同日历锚点起,历年价差先朝**不利**
-- 方向走的最大幅度(MAE)。120 个合格段验证:
--   · MAE 中位 = 补仓参考(浮亏到这里是历年常态,不是逻辑坏了);
--   · MAE 最大 = 风险预留(仓位 = 可承受亏损 ÷ 此数×点值,扛得住最坏年份)。
-- **盈亏比分级(move/MAE)明确不做**:按《体系》分档回测,">2.5 重点档"实际持到底
-- 中位 −0.2% 全场最差(疑与临近交割混杂)——比值不用,分母有用。
--
-- 口径与 revert_* 同一条管线:同月份组合模板、按月-日锚点、只用已走完年份;
-- 高位段 MAE = 锚点后最高价差 − 锚点价差(继续冲高的幅度),低位对称;按构造 ≥ 0。
-- 幂等:同 0001/0003/0004/0005 的教训。

begin;

alter table spread_monitor_daily
    add column if not exists revert_high_mae numeric,
    add column if not exists revert_high_mae_max numeric,
    add column if not exists revert_low_mae numeric,
    add column if not exists revert_low_mae_max numeric;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_revert_mae_sane,
    add constraint spread_monitor_daily_revert_mae_sane
        check ((revert_high_mae is null or revert_high_mae >= 0)
           and (revert_high_mae_max is null or revert_high_mae_max >= revert_high_mae)
           and (revert_low_mae is null or revert_low_mae >= 0)
           and (revert_low_mae_max is null or revert_low_mae_max >= revert_low_mae));

insert into schema_versions (version, description)
values ('202608170006',
        'Spread monitor: historical MAE median and max per side, the sizing denominator behind the risk-budget display')
on conflict (version) do nothing;

commit;
