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
/// `calendar` 是**行情的完整交易日历**,用来补回全员掉榜的那些天。
///
/// 本函数原先只输出「至少一家在榜」的交易日,注释里写的是「与其画一个零,不如让
/// 曲线断在那里」。**那个说法已被运营者 2026-08-16 的拍板推翻**(DEC-061 掉榜日
/// 展示口径):掉榜且反推不出的日子按 0 计入展示曲线、折线不留缺口,靠掉榜底色与
/// 小窗说明把「这是掉榜不是清仓」讲清楚;成本与盈亏仍走三态口径,0 不进成本引擎。
/// 前端(SeatsView)早就按这条实现了,只是**从来没收到过这些天**。
///
/// 代价比「断一格」大得多:日期轴是 category 轴,缺的那天不是留空,而是整列消失,
/// **K 线跟着一起没**——行情数据明明完整。高盛(库里旧名乾坤期货)在黄金上
/// 2026-03-26~04-24 连续掉榜 21 个交易日,页面上那段就没有蜡烛,运营者两次发现。
/// 完全相同的缺陷 2026-08-16 在建仓过程汇总档修过一次(`157a8d4`),DEC-080 把页面
/// 形态换成净持仓之后,新路径又照着席位行造了一遍轴。**这次两条路都有测试钉住。**
///
/// 只补**席位序列首尾之间**缺的交易日:首尾之外那家本来就不在场,补出来是无中生有。
pub fn build_net_position_series(
    rows: &[SeatContractDay],
    selected: &[String],
    calendar: &[Date],
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

    // 补回首尾之间全员掉榜的交易日。`Accum::default()` 手数为零、`present` 为空,
    // 于是下面 `missing` 自然等于全部选中席位——界面据此画掉榜底色并注明。
    if let (Some(&first), Some(&last)) = (by_date.keys().next(), by_date.keys().next_back()) {
        for &date in calendar {
            if date > first && date < last {
                by_date.entry(date).or_default();
            }
        }
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

    /// 连续若干个交易日的行情日历。
    fn calendar(days: &[u8]) -> Vec<Date> {
        days.iter().map(|&n| day(n)).collect()
    }

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
        let series = build_net_position_series(&rows, &["中信".into(), "国泰".into()], &[]);
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
        let series = build_net_position_series(&rows, &["中信".into(), "海通".into()], &[]);
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
        let series = build_net_position_series(&rows, &selected, &[]);
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
        let series = build_net_position_series(&rows, &["中信".into()], &[]);
        assert_eq!(series[0].net_position, Decimal::ZERO);
        assert_eq!(series[0].counted_members, vec!["中信".to_string()]);
        assert!(series[0].missing_members.is_empty());
    }

    #[test]
    fn a_seat_holding_several_contracts_is_summed_per_contract() {
        // 同一家在两个合约上各有仓：逐合约算净再相加，与建仓过程的合约汇总同口径。
        let rows = vec![row("中信", 3, 1000, 0), row("中信", 3, 0, 300)];
        let series = build_net_position_series(&rows, &["中信".into()], &[]);
        assert_eq!(series[0].long_lots, Decimal::from(1000));
        assert_eq!(series[0].short_lots, Decimal::from(300));
        assert_eq!(series[0].net_position, Decimal::from(700));
    }

    #[test]
    fn 没有行情日历时全员掉榜那天不会凭空出现() {
        // **这条只在拿不到日历时成立。** 它原名
        // `a_day_where_everyone_is_off_the_board_simply_does_not_appear`,
        // 注释写的是「与其画一个零,不如让曲线断在那里」——那个口径 2026-08-16 已被
        // 运营者推翻(DEC-061),补轴之后掉榜日要按 0 进展示曲线。留着这条是为了钉住
        // 「日历为空 → 不凭空造日期」这半边,别再照着旧名字理解成「掉榜日就该消失」。
        // 正例见 `全员掉榜的交易日要补回来而不是从轴上消失`。
        let rows = vec![row("中信", 3, 1000, 0), row("中信", 5, 900, 0)];
        let series = build_net_position_series(&rows, &["中信".into()], &[]);
        assert_eq!(series.len(), 2);
        assert_eq!(series[0].trade_date, day(3));
        assert_eq!(series[1].trade_date, day(5));
    }

    #[test]
    fn 全员掉榜的交易日要补回来而不是从轴上消失() {
        // 高盛(乾坤期货)黄金 2026-03-26~04-24 的缩影:在榜 → 连续掉榜 → 回榜。
        // 缺的那几天如果不进序列,前端 category 轴会整列跳过,**K 线跟着一起没**,
        // 而行情数据明明是完整的。运营者两次发现同一个症状(157a8d4 修的是另一条路)。
        let rows = vec![row("中信", 3, 100, 0), row("中信", 7, 80, 0)];
        let series =
            build_net_position_series(&rows, &["中信".into()], &calendar(&[3, 4, 5, 6, 7]));

        let dates: Vec<u8> = series.iter().map(|d| d.trade_date.day()).collect();
        assert_eq!(dates, vec![3, 4, 5, 6, 7], "掉榜的 4/5/6 三天必须在轴上");

        let off = &series[1];
        assert_eq!(off.net_position, Decimal::ZERO);
        assert!(off.counted_members.is_empty(), "掉榜日没有任何席位计入");
        assert_eq!(
            off.missing_members,
            vec!["中信".to_string()],
            "要点名是谁掉了"
        );
    }

    #[test]
    fn 只补首尾之间不向两头外推() {
        // 首尾之外那家本来就不在场,补出来是无中生有 —— 会画出一段他从未持有的仓。
        let rows = vec![row("中信", 5, 100, 0), row("中信", 6, 100, 0)];
        let series =
            build_net_position_series(&rows, &["中信".into()], &calendar(&[3, 4, 5, 6, 7, 10]));
        let dates: Vec<u8> = series.iter().map(|d| d.trade_date.day()).collect();
        assert_eq!(dates, vec![5, 6], "两头一天都不许补");
    }

    #[test]
    fn 补轴不改变在榜日的任何数字() {
        // 补轴只该多出几行,已有的行一个字段都不能动。
        let rows = vec![row("中信", 3, 100, 20), row("中信", 7, 80, 0)];
        let bare = build_net_position_series(&rows, &["中信".into()], &[]);
        let filled =
            build_net_position_series(&rows, &["中信".into()], &calendar(&[3, 4, 5, 6, 7]));
        let kept: Vec<_> = filled
            .iter()
            .filter(|d| !d.counted_members.is_empty())
            .collect();
        assert_eq!(kept.len(), bare.len());
        for (a, b) in bare.iter().zip(kept) {
            assert_eq!(a.trade_date, b.trade_date);
            assert_eq!(a.net_position, b.net_position);
            assert_eq!(a.long_lots, b.long_lots);
            assert_eq!(a.short_lots, b.short_lots);
            assert_eq!(a.counted_members, b.counted_members);
        }
    }

    #[test]
    fn 日历为空时退回原行为() {
        // 调用方拿不到行情日历时不能崩,也不能凭空造日期。
        let rows = vec![row("中信", 3, 100, 0), row("中信", 7, 80, 0)];
        let series = build_net_position_series(&rows, &["中信".into()], &[]);
        let dates: Vec<u8> = series.iter().map(|d| d.trade_date.day()).collect();
        assert_eq!(dates, vec![3, 7]);
    }
}
