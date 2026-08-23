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
    /// 交易所当天**没有公布**这个合约(或品种)的持仓排名(DEC-130):大商所只对
    /// 持仓量 ≥ 2 万手的合约发排名,合约临近到期跌破 2 万手后排名停发 —— 这不是
    /// 席位掉榜,是整张榜不存在。界面要与「掉榜」分开说,净持仓留空而不是画 0。
    pub unpublished: bool,
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
                unpublished: false,
            }
        })
        .collect()
}

/// 把「交易所未公布排名」的日子标出来,并把席位序列**末尾之后**仍有行情的交易日补上(DEC-130)。
///
/// 运营者 2026-08-23:生猪 LH2607 的 K 线与持仓在 6/23 之后整个消失,以为席位全掉榜了。
/// 查实是大商所**只对持仓量 ≥ 2 万手的合约公布成交持仓排名**:6/24 它跌到 19,583 手,
/// 排名停发,席位数据自然断在那里;而行情一直有到 7/22、散户窗口止点 6/30 —— 最后一周
/// 在页面上是看不见的。上面 `build_net_position_series` 只补首尾之间,尾巴之后一天不补
/// (那是防「无中生有」),所以这里单独处理尾巴:
///   · 尾巴上每个有行情的交易日补一行:手数 0、`missing` = 全部选中席位;
///   · 无论首尾之间还是尾巴上,**`published` 里没有的日子标 `unpublished = true`**
///     —— 那天交易所根本没发这张榜,不是谁掉了榜。
/// `published` = 这个合约(或品种)在库里有**任何**席位行的交易日集合,由取数层给。
pub fn mark_unpublished_and_extend_tail(
    mut series: Vec<NetPositionDay>,
    selected: &[String],
    calendar: &[Date],
    published: &BTreeSet<Date>,
) -> Vec<NetPositionDay> {
    if let Some(last) = series.last().map(|d| d.trade_date) {
        for &date in calendar {
            if date > last {
                series.push(NetPositionDay {
                    trade_date: date,
                    net_position: Decimal::ZERO,
                    long_lots: Decimal::ZERO,
                    short_lots: Decimal::ZERO,
                    counted_members: Vec::new(),
                    missing_members: selected.to_vec(),
                    unpublished: false,
                });
            }
        }
    }
    for day in series.iter_mut() {
        day.unpublished = !published.contains(&day.trade_date);
    }
    series
}

/// 选中那个交易日在序列里对应的那一天(含当天;没有正好那天就退到之前最近的一天)。
///
/// 席位页顶上写着「选几个会员和一个交易日,**两个子页共用这组选择**」,但净持仓
/// 这一路从来没接过日期(`SeatNetPositionQuery` 里根本没有这个字段),摘要永远报
/// 序列最后一天。运营者 2026-08-20 选了 8.19,摘要那行仍写 2026-08-20,当场发现。
///
/// **只决定「摘要与各家分腿看哪一天」,不动序列本身。**
/// 我第一版把整条序列截到选中日为止,运营者当场否掉:
/// 「应该只改净持仓的多单空单显示,就是改上面的文字,方便我看各家情况,其他全部不用变」。
/// 他要的是**一边看某天的各家明细、一边保留完整的图**——K 线、净持仓曲线、累计盈亏
/// 都是上下文,截掉等于把上下文一起拿走了。
///
/// `as_of` 为 `None` 时给最后一天 —— 没选日期就是「看最新」。
/// 选中日早于全部数据时给 `None`:那天他确实没有持仓可看,摘要留空比退回最新诚实
/// (退回最新等于默默无视选择,那正是这次要修的毛病)。
pub fn as_of_day(series: &[NetPositionDay], as_of: Option<Date>) -> Option<Date> {
    match as_of {
        Some(end) => series
            .iter()
            .rev()
            .find(|day| day.trade_date <= end)
            .map(|day| day.trade_date),
        None => series.last().map(|day| day.trade_date),
    }
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
    fn 尾巴之后有行情的交易日补上并标交易所未公布() {
        // LH2607:席位到 6/23,行情到 7/22,交易所 6/24 起停发排名(持仓 <2 万手)。
        let rows = vec![row("中信", 3, 100, 0), row("中信", 4, 100, 0)];
        let base = build_net_position_series(&rows, &["中信".into()], &calendar(&[3, 4, 5, 6, 7]));
        let published: BTreeSet<Date> = [day(3), day(4)].into_iter().collect();
        let out = mark_unpublished_and_extend_tail(
            base,
            &["中信".into()],
            &calendar(&[3, 4, 5, 6, 7]),
            &published,
        );
        let dates: Vec<u8> = out.iter().map(|d| d.trade_date.day()).collect();
        assert_eq!(dates, vec![3, 4, 5, 6, 7], "尾巴上的行情日要补上");
        assert!(!out[0].unpublished && !out[1].unpublished, "有榜的日子不标");
        assert!(
            out[2].unpublished && out[4].unpublished,
            "没榜的日子标未公布"
        );
        assert_eq!(out[2].missing_members, vec!["中信".to_string()]);
        assert_eq!(out[2].net_position, Decimal::ZERO);
    }

    #[test]
    fn 首尾之间全员掉榜但交易所有发榜的日子不算未公布() {
        // 那天榜是有的,只是这家不在前二十 —— 这是掉榜,不是未公布。
        let rows = vec![row("中信", 3, 100, 0), row("中信", 5, 100, 0)];
        let base = build_net_position_series(&rows, &["中信".into()], &calendar(&[3, 4, 5]));
        let published: BTreeSet<Date> = [day(3), day(4), day(5)].into_iter().collect();
        let out = mark_unpublished_and_extend_tail(
            base,
            &["中信".into()],
            &calendar(&[3, 4, 5]),
            &published,
        );
        assert!(!out[1].unpublished);
        assert_eq!(out[1].missing_members, vec!["中信".to_string()]);
    }

    #[test]
    fn 日历为空时退回原行为() {
        // 调用方拿不到行情日历时不能崩,也不能凭空造日期。
        let rows = vec![row("中信", 3, 100, 0), row("中信", 7, 80, 0)];
        let series = build_net_position_series(&rows, &["中信".into()], &[]);
        let dates: Vec<u8> = series.iter().map(|d| d.trade_date.day()).collect();
        assert_eq!(dates, vec![3, 7]);
    }

    fn series_of(days_: &[u8]) -> Vec<NetPositionDay> {
        days_
            .iter()
            .map(|&n| NetPositionDay {
                trade_date: day(n),
                net_position: Decimal::from(n),
                long_lots: Decimal::ZERO,
                short_lots: Decimal::ZERO,
                counted_members: vec![],
                missing_members: vec![],
                unpublished: false,
            })
            .collect()
    }

    #[test]
    fn 选中当天就取当天() {
        let s = series_of(&[3, 4, 5, 6, 7]);
        assert_eq!(as_of_day(&s, Some(day(5))), Some(day(5)));
    }

    #[test]
    fn 没选日期就取最后一天() {
        // 没选 = 看最新,这是默认行为,加了参数也不能把它改掉。
        let s = series_of(&[3, 4, 5]);
        assert_eq!(as_of_day(&s, None), Some(day(5)));
    }

    #[test]
    fn 选了非交易日就退到之前最近的一天() {
        // 用 `<=` 而不是相等:选到周末或休市日时给「截至那时」,不是给一片空白。
        let s = series_of(&[3, 4, 7, 10]);
        assert_eq!(as_of_day(&s, Some(day(9))), Some(day(7)));
    }

    #[test]
    fn 选的日期早于全部数据时给空而不是退回最新() {
        // 退回最新等于默默无视用户的选择,而那正是这次要修的毛病。
        let s = series_of(&[5, 6]);
        assert_eq!(as_of_day(&s, Some(day(1))), None);
    }

    #[test]
    fn 不动序列本身() {
        // **这条钉的是范围**:运营者要的是只改摘要那行,图要保持完整。
        // 第一版把整条序列截掉被他当场否掉,这里防止有人再截一次。
        let s = series_of(&[3, 4, 5, 6, 7]);
        let before = s.len();
        let _ = as_of_day(&s, Some(day(4)));
        assert_eq!(s.len(), before, "as_of_day 只能挑一天出来,不许改动序列");
    }
}
