//! 席位净持仓成本：建仓过程那条成本线怎么算出来的。
//!
//! 口径由运营者定（`docs/SEAT_AND_SPREAD_REQUIREMENTS.md`），逐条对应到下面的实现：
//!
//! * **净持仓计价，多空不分开记账** —— 与三禾一致，运营者拍板。
//! * **成本基准用结算价**，不是收盘价。国内期货的结算价定义就是当日成交价按成交量的
//!   加权平均，它本身就是那天所有真实成交的加权均价。实测焦煤 2015 全年 845 个有成交
//!   合约日，结算价 844 次落在当日最高最低之间；以结算价为基准，收盘价的中位偏离达当日
//!   区间的 25%，而且偏离有方向性，趋势日里不会自我抵消。
//! * **加仓按加权平均累积，减仓不改均价** —— 减仓兑现的是盈亏，不改变剩下那部分的成本。
//! * **净头寸翻向时成本重置** —— 跨符号做加权平均没有意义：净多两百手和净空两百手不是
//!   同一笔仓位的延续，是先平掉再反向建立。
//! * **盈亏用 `price_multiplier`，不是合约单位** —— 八个品种里只有鸡蛋两者不等
//!   （合约 5 吨但报价按 500 千克），用错会正好差一倍，而数字看着完全正常。
//!
//! 字段名用「净持仓成本（推算）」而不是「成交均价」：我们看不到成交明细，这是由公开的
//! 持仓变化与结算价推出来的，不是真实成交价。

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use serde::Serialize;
use time::Date;
use utoipa::ToSchema;

/// 某席位在某合约某日的持仓与当日结算价。
#[derive(Debug, Clone, PartialEq)]
pub struct DailyPosition {
    pub trade_date: Date,
    /// 净持仓：持买减持卖。正为净多，负为净空。
    ///
    /// **`None` 表示那天不知道，不是零。** 交易所只公布前二十名，该席位掉出榜单的
    /// 日子官方文件里根本没有他这一行。把缺失当零，成本引擎会读成「这笔仓位平了」，
    /// 于是清掉成本、重新起算——2026-07-29 高盛焦煤那种掉一天又回来的情形，
    /// 成本线会凭空断开，当日盈亏也会算成一次巨额平仓。
    pub net_position: Option<Decimal>,
    /// 当日结算价。没有则该日成本不可知——零成交日的结算价是推导值不是真实成交。
    pub settlement: Option<Decimal>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct CostPoint {
    #[serde(serialize_with = "crate::spread_analytics::date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub trade_date: Date,
    #[schema(value_type = f64)]
    /// `None` 表示那天该席位不在榜上，持仓未知——与「持仓为零」是两回事。
    pub net_position: Option<Decimal>,
    /// 净持仓成本（推算）。仓位为零、或建仓当日无结算价时为空。
    #[schema(value_type = Option<f64>)]
    pub cost: Option<Decimal>,
    /// 当日盈亏 =（今结算 − 昨结算）× 昨净持仓 × price_multiplier。
    /// 用的是持仓在手期间的价格变动，不是与成本的差额——后者是浮动盈亏，不是当日盈亏。
    #[schema(value_type = Option<f64>)]
    pub daily_pnl: Option<Decimal>,
    /// 浮动盈亏 =（今结算 − 成本）× 今净持仓 × price_multiplier。
    #[schema(value_type = Option<f64>)]
    pub open_pnl: Option<Decimal>,
    /// 该合约自序列开头至今的当日盈亏累计。
    ///
    /// **不可知的那几天按 0 计入而不是中断累计**：当日盈亏不可知（掉出前 20、
    /// 零成交无结算价）意味着那天赚了多少我们不知道，但累计线不该因此归零或断掉——
    /// 断掉会让人以为仓位平了。界面上用 `daily_pnl` 为空标出那几天，累计线如实
    /// 说明它是「已知部分的累计」。
    #[schema(value_type = f64)]
    pub cumulative_pnl: Decimal,
    /// 该日成本不可知的原因，供界面如实标注而不是画一条假线。
    pub cost_unknown_reason: Option<&'static str>,
}

/// 逐日推算净持仓成本。输入必须按交易日升序，且同一席位同一合约。
pub fn build_cost_series(points: &[DailyPosition], price_multiplier: Decimal) -> Vec<CostPoint> {
    let mut out = Vec::with_capacity(points.len());
    let mut cost: Option<Decimal> = None;
    let mut previous_net = Decimal::ZERO;
    let mut previous_settlement: Option<Decimal> = None;
    let mut cumulative = Decimal::ZERO;

    for point in points {
        // 那天不知道他持了多少：**什么状态都不动**。
        //
        // 不能按零处理（会被下面读成平仓，清掉成本并算出一次假的巨额盈亏），
        // 也不能沿用前值假装没变（那是编数据）。已知的成本、上一日净持仓、上一日
        // 结算价原样保留，等有数据的那天接着算；这一天的成本与盈亏如实报未知。
        let Some(net) = point.net_position else {
            out.push(CostPoint {
                trade_date: point.trade_date,
                net_position: None,
                cost: None,
                daily_pnl: None,
                open_pnl: None,
                cost_unknown_reason: Some("seat_off_the_board"),
                cumulative_pnl: cumulative,
            });
            // previous_settlement 仍然跟进：结算价来自行情，与他上不上榜无关。
            previous_settlement = point.settlement.or(previous_settlement);
            continue;
        };
        let mut reason: Option<&'static str> = None;

        // 翻向或归零：这笔仓位结束了，成本不再延续。
        let flipped = (previous_net > Decimal::ZERO && net < Decimal::ZERO)
            || (previous_net < Decimal::ZERO && net > Decimal::ZERO);
        if flipped || net.is_zero() {
            cost = None;
        }

        if net.is_zero() {
            let today = daily_pnl(
                previous_net,
                previous_settlement,
                point.settlement,
                price_multiplier,
            );
            cumulative += today.unwrap_or(Decimal::ZERO);
            out.push(CostPoint {
                trade_date: point.trade_date,
                net_position: Some(net),
                cost: None,
                daily_pnl: today,
                open_pnl: None,
                cost_unknown_reason: None,
                cumulative_pnl: cumulative,
            });
            previous_net = net;
            previous_settlement = point.settlement.or(previous_settlement);
            continue;
        }

        // 加仓的部分：翻向后从零起算，否则只看绝对值的增量。
        let previous_abs = if flipped {
            Decimal::ZERO
        } else {
            previous_net.abs()
        };
        let added = net.abs() - previous_abs;

        if added > Decimal::ZERO {
            match point.settlement {
                Some(settlement) => {
                    cost = Some(match cost {
                        // 减仓不改均价，所以旧成本对应的是 previous_abs 那部分。
                        Some(existing) => {
                            (existing * previous_abs + settlement * added) / net.abs()
                        }
                        None => settlement,
                    });
                }
                None => {
                    // 建仓当日没有结算价，这笔仓位的成本从此不可知。硬算一个数出来，
                    // 会让后面每一天的成本线都带着一个编出来的起点。
                    cost = None;
                    reason = Some("no_settlement_on_add");
                }
            }
        }

        let open_pnl = match (cost, point.settlement) {
            (Some(c), Some(s)) => Some((s - c) * net * price_multiplier),
            _ => None,
        };
        if cost.is_none() && reason.is_none() {
            reason = Some("position_opened_before_data");
        }

        let today = daily_pnl(
            previous_net,
            previous_settlement,
            point.settlement,
            price_multiplier,
        );
        cumulative += today.unwrap_or(Decimal::ZERO);
        out.push(CostPoint {
            trade_date: point.trade_date,
            net_position: Some(net),
            cost,
            daily_pnl: today,
            open_pnl,
            cost_unknown_reason: reason,
            cumulative_pnl: cumulative,
        });
        previous_net = net;
        previous_settlement = point.settlement.or(previous_settlement);
    }
    out
}

fn daily_pnl(
    previous_net: Decimal,
    previous_settlement: Option<Decimal>,
    settlement: Option<Decimal>,
    price_multiplier: Decimal,
) -> Option<Decimal> {
    match (previous_settlement, settlement) {
        (Some(before), Some(now)) if !previous_net.is_zero() => {
            Some((now - before) * previous_net * price_multiplier)
        }
        _ => None,
    }
}

/// 品种汇总的一天：该席位在这个品种**全部合约**上的持仓合并之后的样子。
///
/// 合约按当日净方向分成两组：净多的那些合约手数相加是「多单」，净空的那些是
/// 「空单」，两者相减才是真正的净持仓。均价各按手数加权——运营者要看的正是
/// 「净多的那几个合约均价多少、净空的那几个均价多少」。
///
/// **为什么逐合约算完再合并，而不是直接用交易所的「品种汇总榜」那一行**——
/// 成本是按合约推的（每个合约有自己的结算价与建仓节奏），汇总榜只给一个总手数，
/// 推不出成本，更分不出多空两腿。
///
/// 代价要说清楚：汇总榜覆盖该席位在**整个品种**上的排名，逐合约行只覆盖他进了
/// **某个合约前二十**的部分。永安黄金 3316 个交易日实测，两者完全相等 2722 天
/// （82%），平均差 55.6 手、最大 2120 手——差的是他确实持有、但在那个合约上排
/// 不进前二十的零头。页面说明里写明了这一点，不当作等同。
#[derive(Debug, Clone)]
pub struct VarietyDay {
    pub trade_date: Date,
    /// 当日净多的那些合约，手数相加。
    pub long_lots: Decimal,
    /// 上面那些合约的净持仓成本，按手数加权。
    pub long_cost: Option<Decimal>,
    /// `long_cost` 实际覆盖到的手数。小于 `long_lots` 说明有合约的成本不可知
    /// （建仓当日无结算价、或数据起点之前就持有），那这个均价只是**已知部分**
    /// 的均价。界面据此标注，而不是让人以为它覆盖了全部持仓。
    pub long_cost_lots: Decimal,
    pub short_lots: Decimal,
    pub short_cost: Option<Decimal>,
    pub short_cost_lots: Decimal,
    /// 净持仓 = 净多手数 − 净空手数。
    pub net_position: Decimal,
    /// 各合约当日盈亏之和。所有合约都不可知时为空。
    pub daily_pnl: Option<Decimal>,
    /// 上面那个的逐日累加。**必须在这里重算，不能把各合约的 `cumulative_pnl`
    /// 相加**：合约到期之后就没有行了，相加会让它已实现的那部分凭空消失，
    /// 累计线在每次移仓时往下掉一截。
    pub cumulative_pnl: Decimal,
}

/// 把同一席位在一个品种下各个合约的持仓序列，卷成逐日的品种汇总。
///
/// 每个合约先各自走 [`build_cost_series`]（成本、当日盈亏的口径与单合约页
/// 完全一致，不另起一套），再按交易日归并。入参每一项是一个合约的序列，
/// 必须按交易日升序。
///
/// 只喂该合约**真正有行的那些天**：这里没有「掉榜」的概念——某合约某天没有行，
/// 既可能是掉出该合约前二十，也可能是这个合约还没挂牌或已经到期，两者从数据上
/// 分不开。当作缺行处理即可，那天它不贡献手数，成本与上一日结算价原地冻结。
pub fn build_variety_series(
    contracts: &[Vec<DailyPosition>],
    price_multiplier: Decimal,
) -> Vec<VarietyDay> {
    #[derive(Default)]
    struct Accum {
        long_lots: Decimal,
        long_weighted: Decimal,
        long_cost_lots: Decimal,
        short_lots: Decimal,
        short_weighted: Decimal,
        short_cost_lots: Decimal,
        daily_pnl: Option<Decimal>,
    }

    let mut by_date: BTreeMap<Date, Accum> = BTreeMap::new();
    for points in contracts {
        for point in build_cost_series(points, price_multiplier) {
            let slot = by_date.entry(point.trade_date).or_default();
            if let Some(pnl) = point.daily_pnl {
                slot.daily_pnl = Some(slot.daily_pnl.unwrap_or(Decimal::ZERO) + pnl);
            }
            // 那天这个合约的持仓不可知：不贡献手数，也不当成零。
            let Some(net) = point.net_position else {
                continue;
            };
            if net > Decimal::ZERO {
                slot.long_lots += net;
                // 成本不可知的合约照样计手数，但不进均价——否则要么少算手数，
                // 要么拿一个编出来的成本去拉均价。两个数分开报最诚实。
                if let Some(cost) = point.cost {
                    slot.long_weighted += cost * net;
                    slot.long_cost_lots += net;
                }
            } else if net < Decimal::ZERO {
                let lots = -net;
                slot.short_lots += lots;
                if let Some(cost) = point.cost {
                    slot.short_weighted += cost * lots;
                    slot.short_cost_lots += lots;
                }
            }
        }
    }

    let mut cumulative = Decimal::ZERO;
    by_date
        .into_iter()
        .map(|(trade_date, slot)| {
            cumulative += slot.daily_pnl.unwrap_or(Decimal::ZERO);
            VarietyDay {
                trade_date,
                long_cost: (slot.long_cost_lots > Decimal::ZERO)
                    .then(|| slot.long_weighted / slot.long_cost_lots),
                short_cost: (slot.short_cost_lots > Decimal::ZERO)
                    .then(|| slot.short_weighted / slot.short_cost_lots),
                net_position: slot.long_lots - slot.short_lots,
                long_lots: slot.long_lots,
                long_cost_lots: slot.long_cost_lots,
                short_lots: slot.short_lots,
                short_cost_lots: slot.short_cost_lots,
                daily_pnl: slot.daily_pnl,
                cumulative_pnl: cumulative,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::macros::date;

    fn day(d: Date, net: i64, settlement: Option<Decimal>) -> DailyPosition {
        DailyPosition {
            trade_date: d,
            net_position: Some(Decimal::from(net)),
            settlement,
        }
    }

    /// 该席位那天不在榜上：持仓未知。
    fn absent(d: Date, settlement: Option<Decimal>) -> DailyPosition {
        DailyPosition {
            trade_date: d,
            net_position: None,
            settlement,
        }
    }

    #[test]
    fn a_day_off_the_board_is_unknown_and_must_not_reset_the_cost() {
        // 生产实例：高盛 AU2610 多头 07-28 在榜 2038、07-29 掉出前二十、07-30 回榜 2416。
        // 把 07-29 当成「持仓 0」，引擎会读成平仓：清掉成本、当日盈亏算出一次巨额了结，
        // 07-30 再从头起算——成本线断开，而图上看不出这是数据缺失还是真的平了。
        let points = vec![
            day(date!(2026 - 07 - 27), 10, Some(Decimal::from(100))),
            day(date!(2026 - 07 - 28), 10, Some(Decimal::from(110))),
            absent(date!(2026 - 07 - 29), Some(Decimal::from(120))),
            day(date!(2026 - 07 - 30), 10, Some(Decimal::from(130))),
        ];
        let series = build_cost_series(&points, Decimal::ONE);

        // 掉榜那天：持仓、成本、盈亏一律未知，并说明原因。
        assert_eq!(series[2].net_position, None);
        assert_eq!(series[2].cost, None);
        assert_eq!(series[2].daily_pnl, None);
        assert_eq!(series[2].cost_unknown_reason, Some("seat_off_the_board"));

        // 回榜那天成本仍然是 100，没有被重置成 130。
        assert_eq!(series[3].cost, Some(Decimal::from(100)));
        assert_eq!(series[3].net_position, Some(Decimal::from(10)));

        // 累计盈亏在未知那天不动，回榜后接着按结算价推进：
        // 07-28 相对 100 涨 10 → +100；07-29 未知不计；07-30 相对 120 涨 10 → +100。
        assert_eq!(series[1].cumulative_pnl, Decimal::from(100));
        assert_eq!(series[2].cumulative_pnl, Decimal::from(100));
        assert_eq!(series[3].cumulative_pnl, Decimal::from(200));
    }

    #[test]
    fn a_real_zero_still_ends_the_position() {
        // 未知不等于零，但零仍然是零：真的平到零，成本就该结束。
        let points = vec![
            day(date!(2026 - 07 - 27), 10, Some(Decimal::from(100))),
            day(date!(2026 - 07 - 28), 0, Some(Decimal::from(110))),
            day(date!(2026 - 07 - 29), 10, Some(Decimal::from(120))),
        ];
        let series = build_cost_series(&points, Decimal::ONE);
        assert_eq!(series[1].net_position, Some(Decimal::ZERO));
        assert_eq!(series[1].cost, None);
        // 平掉之后再建仓，成本从新的结算价起算，不是旧的 100。
        assert_eq!(series[2].cost, Some(Decimal::from(120)));
    }

    #[test]
    fn the_cumulative_line_is_the_running_sum_and_unknown_days_do_not_break_it() {
        // 累计盈亏 = 已知当日盈亏的累加。不可知的那天按 0 计入而不是中断——
        // 中断会让累计线断开或归零，看上去像仓位平了，那是另一回事。
        let points = vec![
            day(date!(2026 - 01 - 05), 10, Some(Decimal::from(100))),
            day(date!(2026 - 01 - 06), 10, Some(Decimal::from(110))), // +10 × 10 × 1
            day(date!(2026 - 01 - 07), 10, None),                     // 不可知
            day(date!(2026 - 01 - 08), 10, Some(Decimal::from(115))), // 相对 110：+5 × 10
        ];
        let series = build_cost_series(&points, Decimal::ONE);
        assert_eq!(series[0].cumulative_pnl, Decimal::ZERO);
        assert_eq!(series[1].cumulative_pnl, Decimal::from(100));
        // 不可知那天：当日为空，累计原地不动，不断开。
        assert_eq!(series[2].daily_pnl, None);
        assert_eq!(series[2].cumulative_pnl, Decimal::from(100));
        assert_eq!(series[3].cumulative_pnl, Decimal::from(150));
        // 累计必须等于各已知当日之和，不是重新算的另一个数。
        let summed: Decimal = series.iter().filter_map(|p| p.daily_pnl).sum();
        assert_eq!(series.last().unwrap().cumulative_pnl, summed);
    }

    #[test]
    fn adding_averages_and_reducing_leaves_the_average_alone() {
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 100, Some(Decimal::from(1000))),
                // 再加一百手，成本是两次的加权平均。
                day(date!(2026 - 01 - 06), 200, Some(Decimal::from(1200))),
                // 减到五十手：兑现的是盈亏，剩下那部分的成本不动。
                day(date!(2026 - 01 - 07), 50, Some(Decimal::from(1500))),
            ],
            Decimal::from(60),
        );
        assert_eq!(series[0].cost, Some(Decimal::from(1000)));
        assert_eq!(series[1].cost, Some(Decimal::from(1100)));
        assert_eq!(series[2].cost, Some(Decimal::from(1100)));
    }

    #[test]
    fn flipping_from_long_to_short_starts_a_new_cost() {
        // 净多两百手和净空两百手不是同一笔仓位的延续。跨符号做加权平均，
        // 会算出一个介于两者之间、任何一天都不成立的数。
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 200, Some(Decimal::from(1000))),
                day(date!(2026 - 01 - 06), -150, Some(Decimal::from(1400))),
            ],
            Decimal::from(60),
        );
        assert_eq!(series[0].cost, Some(Decimal::from(1000)));
        assert_eq!(series[1].cost, Some(Decimal::from(1400)));
    }

    #[test]
    fn going_flat_clears_the_cost_and_a_new_position_starts_over() {
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 100, Some(Decimal::from(1000))),
                day(date!(2026 - 01 - 06), 0, Some(Decimal::from(1100))),
                day(date!(2026 - 01 - 07), 80, Some(Decimal::from(1300))),
            ],
            Decimal::from(60),
        );
        assert_eq!(series[1].cost, None);
        assert_eq!(series[2].cost, Some(Decimal::from(1300)));
    }

    #[test]
    fn a_day_without_a_settlement_makes_the_cost_unknown_rather_than_guessed() {
        // 零成交日的结算价是交易所推导出来的，不是真实成交。拿它当建仓成本，
        // 后面每一天的成本线都会带着这个编出来的起点。
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 100, None),
                day(date!(2026 - 01 - 06), 100, Some(Decimal::from(1200))),
            ],
            Decimal::from(60),
        );
        assert_eq!(series[0].cost, None);
        assert_eq!(series[0].cost_unknown_reason, Some("no_settlement_on_add"));
        // 后一天没有加仓，成本仍然不可知，而不是被当天的结算价补上。
        assert_eq!(series[1].cost, None);
    }

    #[test]
    fn daily_profit_uses_the_multiplier_not_the_contract_size() {
        // 鸡蛋是八个品种里唯一两者不等的：合约 5 吨，报价按 500 千克，
        // 所以一元变动值 10 元。用 5 去算，盈亏正好差一倍且看着完全正常。
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 10, Some(Decimal::from(3000))),
                day(date!(2026 - 01 - 06), 10, Some(Decimal::from(3001))),
            ],
            Decimal::from(10),
        );
        assert_eq!(series[1].daily_pnl, Some(Decimal::from(100)));
    }

    #[test]
    fn open_profit_is_measured_against_the_cost_and_daily_against_yesterday() {
        let series = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 100, Some(Decimal::from(1000))),
                day(date!(2026 - 01 - 06), 100, Some(Decimal::from(1010))),
            ],
            Decimal::from(60),
        );
        // 当日盈亏看的是持仓在手期间的价格变动。
        assert_eq!(series[1].daily_pnl, Some(Decimal::from(60000)));
        // 浮动盈亏看的是与成本的差额。
        assert_eq!(series[1].open_pnl, Some(Decimal::from(60000)));
    }

    #[test]
    fn a_position_already_open_before_the_data_starts_is_marked_not_assumed() {
        // 数据起点之前就存在的仓位，成本不可知。运营者要求明确标注，
        // 不静默当零——当零会把整段浮动盈亏算成结算价乘持仓。
        let series = build_cost_series(
            &[day(date!(2026 - 01 - 05), 100, Some(Decimal::from(1000)))],
            Decimal::from(60),
        );
        // 首日即视为加仓，所以这里是有成本的；真正不可知的是下面这种：
        assert_eq!(series[0].cost, Some(Decimal::from(1000)));
        let unknown = build_cost_series(
            &[
                day(date!(2026 - 01 - 05), 100, None),
                day(date!(2026 - 01 - 06), 90, Some(Decimal::from(1000))),
            ],
            Decimal::from(60),
        );
        assert_eq!(unknown[1].cost, None);
        assert_eq!(
            unknown[1].cost_unknown_reason,
            Some("position_opened_before_data")
        );
        assert_eq!(unknown[1].open_pnl, None);
    }

    #[test]
    fn the_variety_total_splits_contracts_by_their_net_direction() {
        // 品种汇总的「多单/空单」不是多头持仓与空头持仓，是**净多的那几个合约**
        // 与**净空的那几个合约**两组。运营者要看的正是这两组各自的手数与均价。
        let near = vec![
            day(date!(2026 - 01 - 05), 100, Some(Decimal::from(900))),
            day(date!(2026 - 01 - 06), 100, Some(Decimal::from(910))),
        ];
        let far = vec![day(date!(2026 - 01 - 06), 300, Some(Decimal::from(950)))];
        let hedge = vec![day(date!(2026 - 01 - 06), -200, Some(Decimal::from(930)))];
        let series = build_variety_series(&[near, far, hedge], Decimal::ONE);

        let today = &series[1];
        assert_eq!(today.long_lots, Decimal::from(400));
        // 减仓不改均价，所以近月还是 900：(900×100 + 950×300) / 400 = 937.5
        assert_eq!(
            today.long_cost,
            Some(Decimal::from(9375) / Decimal::from(10))
        );
        assert_eq!(today.short_lots, Decimal::from(200));
        assert_eq!(today.short_cost, Some(Decimal::from(930)));
        // 净持仓是两组相减，不是某一组。
        assert_eq!(today.net_position, Decimal::from(200));
    }

    #[test]
    fn an_expired_contract_keeps_its_realised_profit_in_the_running_total() {
        // 把各合约的 cumulative_pnl 相加是错的：合约到期之后就没有行了，
        // 它已实现的那部分会凭空消失，累计线在每次移仓时往下掉一截。
        let expiring = vec![
            day(date!(2026 - 01 - 05), 10, Some(Decimal::from(100))),
            day(date!(2026 - 01 - 06), 10, Some(Decimal::from(110))), // +100
        ];
        let rolled_into = vec![
            day(date!(2026 - 01 - 06), 10, Some(Decimal::from(200))),
            day(date!(2026 - 01 - 07), 10, Some(Decimal::from(205))), // +50
        ];
        let series = build_variety_series(&[expiring, rolled_into], Decimal::ONE);

        assert_eq!(series.len(), 3);
        assert_eq!(series[1].cumulative_pnl, Decimal::from(100));
        // 01-07 老合约已经没有行了，它赚的那 100 必须还在累计里。
        assert_eq!(series[2].daily_pnl, Some(Decimal::from(50)));
        assert_eq!(series[2].cumulative_pnl, Decimal::from(150));
    }

    #[test]
    fn a_leg_whose_cost_is_unknown_still_counts_its_lots() {
        // 成本不可知的合约照样是持仓，手数必须算进去；但不能拿一个编出来的成本
        // 去拉均价。手数与「均价覆盖了多少手」分开报，界面才说得清。
        let known = vec![day(date!(2026 - 01 - 05), 100, Some(Decimal::from(900)))];
        let unknown = vec![day(date!(2026 - 01 - 05), 50, None)];
        let series = build_variety_series(&[known, unknown], Decimal::ONE);

        assert_eq!(series[0].long_lots, Decimal::from(150));
        assert_eq!(series[0].long_cost, Some(Decimal::from(900)));
        assert_eq!(series[0].long_cost_lots, Decimal::from(100));
    }

    #[test]
    fn splitting_by_seat_reproduces_the_combined_total() {
        // 净持仓页摘要下面那排是**逐家**的手数与均价，上面那排是合计。两排必须
        // 对得上——分项与总数打架的表，看的人只会两个都不信。
        //
        // 能对上的道理：`build_variety_series` 对「喂进来几条序列」是可加的，
        // 手数直接相加，均价是按手数加权，各家分开算完再加权还原就是合计。
        // 这条测试把这个性质钉死，免得日后有人给分项那条路另写一套算法。
        let seat_a_near = vec![day(date!(2026 - 01 - 05), 100, Some(Decimal::from(900)))];
        let seat_a_far = vec![day(date!(2026 - 01 - 05), -40, Some(Decimal::from(950)))];
        let seat_b = vec![day(date!(2026 - 01 - 05), 60, Some(Decimal::from(1000)))];

        let combined = build_variety_series(
            &[seat_a_near.clone(), seat_a_far.clone(), seat_b.clone()],
            Decimal::ONE,
        );
        let a = build_variety_series(&[seat_a_near, seat_a_far], Decimal::ONE);
        let b = build_variety_series(&[seat_b], Decimal::ONE);

        // 手数：逐家相加 == 合计。
        assert_eq!(a[0].long_lots + b[0].long_lots, combined[0].long_lots);
        assert_eq!(a[0].short_lots + b[0].short_lots, combined[0].short_lots);
        assert_eq!(
            a[0].net_position + b[0].net_position,
            combined[0].net_position
        );

        // 均价：各家按自己覆盖的手数加权，还原出来就是合计的均价。
        let weighted = a[0].long_cost.expect("甲有净多") * a[0].long_cost_lots
            + b[0].long_cost.expect("乙有净多") * b[0].long_cost_lots;
        let lots = a[0].long_cost_lots + b[0].long_cost_lots;
        assert_eq!(weighted / lots, combined[0].long_cost.expect("合计有净多"));

        // 空腿只有甲有，合计的空腿就该等于甲的。
        assert_eq!(combined[0].short_cost, a[0].short_cost);
        assert_eq!(combined[0].short_lots, Decimal::from(40));
    }
}
