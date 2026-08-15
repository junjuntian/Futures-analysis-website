//! 多席位净持仓合计：净持仓页把几家席位的持仓加到一起看。
//!
//! 与 [`crate::seat_cost`] 那条路**刻意分开**。那边算的是一家席位的持仓成本；这边只
//! 做多家的持仓合计，不碰成本——五家机构的仓不是同一笔仓，给它们算一个「平均成本」
//! 会得出一个不对应任何真实仓位的数字。
//!
//! 掉榜按运营者拍板的口径：**不计入，并把当天是谁掉了记下来**。交易所只公布前二十，
//! 某家掉出榜不等于他清仓——按零计会在图上画出一根假的大幅减仓，而那正是看图的人
//! 最容易当成信号的形状。

use rust_decimal::Decimal;
use std::collections::{BTreeMap, BTreeSet};
use time::Date;

/// 取数层给来的一行：某席位在某个合约上某天的多空持仓。
#[derive(Debug, Clone)]
pub struct SeatContractDay {
    /// 归一后的会员名，与调用方传进来的 `selected` 用同一套写法。
    pub member: String,
    pub trade_date: Date,
    pub long: Decimal,
    pub short: Decimal,
}

/// 合计后的一天。
#[derive(Debug, Clone)]
pub struct NetPositionDay {
    pub trade_date: Date,
    /// 所选席位当天的合计净持仓，等于 `long_lots - short_lots`。
    pub net_position: Decimal,
    /// 当天净多的那些「席位×合约」，手数相加。
    ///
    /// 分腿口径与建仓过程的合约汇总一致：**按每个「席位×合约」自己的净方向分组**，
    /// 不是把所有多头持仓相加。净多三千手与净空一千手不是同一笔仓的两部分。
    pub long_lots: Decimal,
    pub short_lots: Decimal,
    /// 当天真正计入的席位。净持仓恰好为零的那家也算在榜——他有行，只是多空相等。
    pub counted_members: Vec<String>,
    /// 当天掉出前二十的席位。**持仓未知，未计入**，界面必须说出来。
    pub missing_members: Vec<String>,
}

#[derive(Default)]
struct Accum {
    long_lots: Decimal,
    short_lots: Decimal,
    present: BTreeSet<String>,
}

/// 把逐 (席位, 合约, 日) 的持仓合成逐日一条序列。
///
/// `selected` 是运营者选中的全部席位，用来算出每天谁掉了榜——只看 `rows` 是看不出
/// 缺席的：缺席在数据上就是「没有这一行」。
///
/// 只有至少一家在榜的交易日才会出现在结果里。全体掉榜的那天没有任何可信的合计值，
/// 与其画一个零，不如让曲线断在那里。
pub fn build_net_position_series(
    rows: &[SeatContractDay],
    selected: &[String],
) -> Vec<NetPositionDay> {
    let mut by_date: BTreeMap<Date, Accum> = BTreeMap::new();
    for row in rows {
        let entry = by_date.entry(row.trade_date).or_default();
        let net = row.long - row.short;
        if net > Decimal::ZERO {
            entry.long_lots += net;
        } else if net < Decimal::ZERO {
            entry.short_lots -= net;
        }
        // 净持仓为零也要记成在榜：他今天有行，只是多空相等。算成掉榜会让界面
        // 报一个不存在的缺席。
        entry.present.insert(row.member.clone());
    }

    by_date
        .into_iter()
        .map(|(trade_date, accum)| {
            let missing = selected
                .iter()
                .filter(|member| !accum.present.contains(*member))
                .cloned()
                .collect();
            NetPositionDay {
                trade_date,
                net_position: accum.long_lots - accum.short_lots,
                long_lots: accum.long_lots,
                short_lots: accum.short_lots,
                counted_members: accum.present.into_iter().collect(),
                missing_members: missing,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::Month;

    fn day(n: u8) -> Date {
        Date::from_calendar_date(2026, Month::August, n).expect("测试日期")
    }

    fn row(member: &str, n: u8, contract_long: i64, contract_short: i64) -> SeatContractDay {
        SeatContractDay {
            member: member.to_string(),
            trade_date: day(n),
            long: Decimal::from(contract_long),
            short: Decimal::from(contract_short),
        }
    }

    #[test]
    fn sums_several_seats_into_one_line() {
        let rows = vec![row("中信", 3, 1000, 200), row("国泰", 3, 500, 100)];
        let series = build_net_position_series(&rows, &["中信".into(), "国泰".into()]);
        assert_eq!(series.len(), 1);
        // 中信净多 800、国泰净多 400，合计 1200。
        assert_eq!(series[0].net_position, Decimal::from(1200));
        assert_eq!(series[0].long_lots, Decimal::from(1200));
        assert_eq!(series[0].short_lots, Decimal::ZERO);
        assert!(series[0].missing_members.is_empty());
    }

    #[test]
    fn splits_legs_by_each_seat_and_contract_own_direction() {
        // 一家净多、一家净空。合计是相减，但两条腿各自记着自己的手数——
        // 把它们先加起来再分方向就丢掉了「谁在多、谁在空」。
        let rows = vec![row("中信", 3, 1000, 0), row("海通", 3, 0, 600)];
        let series = build_net_position_series(&rows, &["中信".into(), "海通".into()]);
        assert_eq!(series[0].long_lots, Decimal::from(1000));
        assert_eq!(series[0].short_lots, Decimal::from(600));
        assert_eq!(series[0].net_position, Decimal::from(400));
    }

    #[test]
    fn a_seat_off_the_board_is_reported_not_counted_as_zero() {
        // 国泰 8-4 没有行 = 掉出前二十，持仓未知。按零计会让合计从 1200 掉到 800，
        // 图上就是一根假的减仓。
        let rows = vec![
            row("中信", 3, 1000, 200),
            row("国泰", 3, 500, 100),
            row("中信", 4, 1000, 200),
        ];
        let selected = vec!["中信".to_string(), "国泰".to_string()];
        let series = build_net_position_series(&rows, &selected);
        assert_eq!(series.len(), 2);
        assert_eq!(series[1].trade_date, day(4));
        assert_eq!(series[1].net_position, Decimal::from(800));
        assert_eq!(series[1].counted_members, vec!["中信".to_string()]);
        assert_eq!(series[1].missing_members, vec!["国泰".to_string()]);
    }

    #[test]
    fn a_flat_seat_still_counts_as_on_the_board() {
        // 多空相等的一家是「在榜且净持仓为零」，不是「掉榜」。混为一谈会让界面
        // 报一个不存在的缺席。
        let rows = vec![row("中信", 3, 500, 500)];
        let series = build_net_position_series(&rows, &["中信".into()]);
        assert_eq!(series[0].net_position, Decimal::ZERO);
        assert_eq!(series[0].counted_members, vec!["中信".to_string()]);
        assert!(series[0].missing_members.is_empty());
    }

    #[test]
    fn a_seat_holding_several_contracts_is_summed_per_contract() {
        // 同一家在两个合约上各有仓：逐合约算净再相加，与建仓过程的合约汇总同口径。
        let rows = vec![row("中信", 3, 1000, 0), row("中信", 3, 0, 300)];
        let series = build_net_position_series(&rows, &["中信".into()]);
        assert_eq!(series[0].long_lots, Decimal::from(1000));
        assert_eq!(series[0].short_lots, Decimal::from(300));
        assert_eq!(series[0].net_position, Decimal::from(700));
    }

    #[test]
    fn a_day_where_everyone_is_off_the_board_simply_does_not_appear() {
        // 全体掉榜那天没有任何可信的合计值，与其画一个零，不如让曲线断在那里。
        let rows = vec![row("中信", 3, 1000, 0), row("中信", 5, 900, 0)];
        let series = build_net_position_series(&rows, &["中信".into()]);
        assert_eq!(series.len(), 2);
        assert_eq!(series[0].trade_date, day(3));
        assert_eq!(series[1].trade_date, day(5));
    }
}
