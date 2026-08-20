//! 套利监控:阈值、拐头、平台位阶梯、到达概率、双向盈亏比。
//!
//! 从 `spread_analytics.rs` 搬来(2026-08-20),**逐行原样搬,没改逻辑**。
//! 用 `use super::*` 而不是逐个 import:搬动本身不该顺带改依赖关系,
//! 那会让「只是挪了个位置」这句话不再成立,出问题时也分不清是搬坏的还是本来就坏。
use super::*;

/// 触发阈值的默认值与边界。
///
/// **5%，不是 10%。** 上线当天在生产的 91 组真实组合上量过（2026-08-11 快照）：
///
/// | 阈值 | 当年触发 | 历年触发 | 合计 |
/// |------|---------|---------|------|
/// | 5%   | 15      | 10      | 25   |
/// | 10%  | 21      | 27      | 47   |
/// | 15%  | 25      | 36      | 56   |
/// | 20%  | 30      | 41      | 61   |
///
/// 10% 那一档是个陡坎：历年触发从 10 跳到 27，合计 47/91 = 52%，半屏飘红等于没报。
/// 设计阶段我推荐过 10%，那是拿**历年轨还没去极端值、且郑商所合约年份还错着**的
/// 数据量的——口径变了，默认值就得重量一遍。去极端值把历年区间收窄，位置自然更
/// 容易落到两端，这是设计使然，不是 bug。
///
/// 上限 0.5 是护栏：到 50% 就是「整个区间都算触发」，那不是阈值而是关掉了过滤。
const MONITOR_THRESHOLD_DEFAULT: f64 = 0.05;
const MONITOR_THRESHOLD_MAX: f64 = 0.50;
/// 合约到期后不再有新快照，它的最后一条会永远留在表里。超过这些天没更新就不算「当前」。
const MONITOR_STALE_DAYS: i32 = 7;

#[derive(Debug, Deserialize)]
pub struct SpreadMonitorQuery {
    /// 落在区间两端多少算触发，0 到 0.5。不传用 0.10。
    pub threshold: Option<f64>,
    /// 看某一天的历史快照。不传看当前。
    pub trade_date: Option<String>,
    /// true = 一次取回全部快照日的行(历史信号视图),忽略 trade_date。
    pub history: Option<bool>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorTrack {
    /// 该口径下的历史最低 / 最高。历年轨是第 2.5 / 97.5 百分位，不是原始极值。
    pub low: String,
    pub high: String,
    /// 当前价差在区间里的位置。历年轨用百分位区间，所以允许落在 0~1 之外。
    pub position: Option<String>,
    pub days: Option<i32>,
    /// "high" / "low" / null。按本次请求的阈值算出来的，不是存下来的。
    pub alert: Option<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorItem {
    pub trade_date: String,
    pub instrument_1: String,
    pub contract_1: String,
    pub instrument_2: String,
    pub contract_2: String,
    pub is_cross_variety: bool,
    pub spread: String,
    pub pair: SpreadMonitorTrack,
    pub years: Option<SpreadMonitorTrack>,
    /// 两条轨里只要有一条触发就不为空。两条方向相反时以「更极端的那条」为准。
    pub alert: Option<String>,
    /// 今天触发、前一交易日按同一阈值不触发 —— 也就是**刚进极值**。
    ///
    /// 焦煤 2026 年有 64% 的交易日都在 3% 触发(价差持续创新低，滚动区间天天被
    /// 刷新)，而连续触发段的中位长度只有 3 日:绝大多数段是短的，长段拖着不放
    /// 才是噪音。区分这两者，页面才能把「新出现的机会」从一片红里挑出来。
    ///
    /// 前一日位置缺失(该组合的第一天、或前一日没有快照)时为 false —— 判不了就
    /// 不打标记，宁可漏标也不假报。
    pub is_new_alert: bool,
    /// 未触发、且未拐头时为空；否则给出**这一行要做的那笔交易**那一侧的历年统计,
    /// 样本不足也为空。侧别 = 拐头侧优先、其次报警侧(DEC-088):⚡ 由拐头触发,
    /// 资格就必须用拐头侧的数字判,否则会拿 A 方向的成绩给 B 方向发通行证。
    /// `side` 同时就是交易方向:"high" = 做空价差,"low" = 做多价差。
    pub revert: Option<SpreadRevertStats>,
    /// **报警侧与拐头侧相反时**,另一侧(报警侧)的统计。方向一致时为空。
    ///
    /// 存在即意味着这一行的两条轨在讲相反的故事(例:历年轨贴底 → 做多价差,
    /// 当年轨自顶部拐头 → 做空价差)。界面必须把它显示出来:DEC-088 那个 BUG 的
    /// 本质就是只显示了其中一侧,让人以为只有一笔交易可做。
    pub revert_alt: Option<SpreadRevertStats>,
    /// 平台位阶梯(DEC-095):价差自己走出来的横盘转折位,按档位从高到低。
    /// 运营者下单看的就是它——「收盘突破平台位,才能继续往下看」。
    /// 空数组 = 那天还没算出档位(序列太短,或旧行没有这一列)。
    pub shelves: Vec<SpreadShelf>,
    /// "high" / "low" / null —— **已拐头**：近 20 个交易日内当年轨曾进 3% 报警带，
    /// 且当前已自极值回撤超过区间宽度的 10%（= 位置退到 0.90 以下 / 0.10 以上）。
    ///
    /// 这是分层规则（DEC-063）的进场信号：报警只是机会出现，拐头才是上车点。
    /// 全样本回放里报警即进持到底中位为负；先按历年统计筛资格、再等拐头，
    /// 留一法验证下持到底中位 +39%。报警带取最严档（3%）、回撤量取通用 10%，
    /// 都是常量不随页面阈值变——给两个可调旋钮只会诱导挑参数。
    /// 只看当年轨：资格统计与回放验证都在当年轨的可交易窗口上，口径闭环。
    pub turn: Option<String>,
    /// **今天刚拐头**：拐头成立，且前一交易日位置还在回撤线的另一侧——位置是今天
    /// 才穿线的。这就是回放里的进场日；拐头标最多挂 20 个交易日,「处于可进场状态」
    /// 与「今天就是进场日」是两回事,界面靠它把后者单独点亮并置顶。
    /// 判定用 `prev_pair_position`(段首日标记的同一素材),前一日缺失时为 false
    /// ——判不了就不标,与 is_new_alert 同一条原则。
    pub is_new_turn: bool,
    /// 拐头侧在近 20 个交易日内的穿线次数(含今天)。≥2 = 同一段行情里拐头反复
    /// ——JM2609/JM2701 八天三次穿线、期间打回区间顶,前两次进场按离场规则都
    /// 止损;FG2701/SA2701 干脆的拐头只有 1。界面用它打「信号差」降级标。
    /// 仅拐头行给值(没拐头谈不上拐头质量)。
    pub turn_crosses: Option<i32>,
    /// 距该组合可交易窗口止点(先到期腿散户最后交易日)的剩余交易日,周内日近似。
    /// 界面按《体系》红线(≤15 清仓/压制进场)与数据实证的衰减区(<40)分档提示。
    pub days_left: Option<i32>,
    /// 该**月份模板**的手工产业备注(DEC-069):运营者手填的品种级知识,
    /// 跟月份走不跟具体合约走(JD2609/2701 与 JD2709/2801 共享「09-01」一条)。
    pub note: Option<String>,
    /// 组合已到期(先到期腿的散户窗口在最新快照日之前已关):历史信号里的
    /// 过期组合打灰标(DEC-071)。按最新快照日判而不是墙钟,结果可复现。
    pub expired: bool,
    /// 该品种当日的现货与基差背景(DEC-074)。跨期两条腿相对同一个现货,
    /// 所以基差之差就是价差本身——这里给的是**水平与历史分位**,是背景不是信号。
    pub basis: Option<SpreadBasisInfo>,
}

/// 现货与基差背景。跨品种组合按第一条腿的品种给(玻纯以玻璃为准)。
#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadBasisInfo {
    pub instrument: String,
    /// 基差数据的交易日。源偶尔缺日,可能早于快照日(最多回看 7 天)。
    pub trade_date: String,
    pub spot_price: String,
    /// 主力基差 = 现货 − 主力期货。为正是期货贴水,为负是期货升水。
    pub dominant_basis: Option<String>,
    pub dominant_basis_rate: Option<String>,
    /// 主力基差率在该品种历年里的百分位(0~1),样本不足 60 天为 None。
    pub percentile: Option<String>,
}

/// 该月份组合模板在**可交易窗口**内、按日历位置对齐的历年表现。
///
/// **不是这一组合自己的胜率**:样本是同品种、同月份对、同年差的模板跨年拼起来的
/// (例如鸡蛋 09-01)。一个具体合约对一辈子只有一个生命周期,算不出有意义的比率。
///
/// 口径(完整版见迁移 202608170002):可交易窗口照 5A 窗口引擎——止点 = 先到期那条
/// 腿的散户最后交易日;历年按**月-日**对齐,一直看到各自窗口的止点;**曾经触及**
/// 即算回归,不比终点。只用已走完的年份实例。
///
/// **单看 rate 会骗人**,所以三个数一起给:剩余期一长 rate 就趋近 100%(任何波动
/// 序列在足够长的窗口里几乎必然回落一次);JD2612/JD2701 的 rate 是 100% 而 drift
/// 中位是 −166 点,方向反的。
#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadRevertStats {
    /// 与本次报警同侧:"low" 统计低位、"high" 统计高位。
    pub side: String,
    /// 曾经触及回归的年数与样本年数。给原始计数是有意的:「12 年里 11 年」比孤零零
    /// 一个 92% 更能让人看出样本有多薄。不设年数门槛,薄不薄由界面写出来让人自己判断。
    pub hit: i32,
    pub n: i32,
    pub rate: String,
    /// 最有利那一刻相对起点走了多少**点**(择时平仓的上限)。价差会跨零,所以不给
    /// 百分比——2019-08-14 起点 −8 点、回落 407 点,百分比是 5000%,毫无意义。
    pub move_points: Option<String>,
    /// 一直持到窗口止点的净变化,已标准化成**正数 = 朝回归走**。
    pub drift_points: Option<String>,
    /// 历年 MAE 中位:锚点后先朝不利方向走的幅度,浮亏到这里是历年常态——
    /// **补仓参考**(《盖楼》猪 11-05 分批法的数据化,DEC-067)。
    pub mae_points: Option<String>,
    /// 历年 MAE 最大:**风险预留**。仓位 = 可承受亏损 ÷ (此数 × 点值)。
    /// 盈亏比分级(move÷MAE)已回测否决(">2.5 档"实际最差),只给分母不给比值。
    pub mae_max_points: Option<String>,
    /// 历年剩余交易日中位数,给上面几个数一个时间尺度。
    pub days: Option<i32>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorResponse {
    /// 本次采用的阈值，原样回给界面——省得界面自己记一份默认值。
    pub threshold: String,
    pub as_of: Option<String>,
    pub available_dates: Vec<String>,
    pub items: Vec<SpreadMonitorItem>,
}

/// 一条轨的触发判定。位置在下端 `threshold` 之内报低位，上端之内报高位。
///
/// **越界也算触发**：历年轨用的是百分位区间，当前价差可以落在第 2.5 百分位之下
/// （位置为负）或第 97.5 之上（位置大于 1）。那是比「贴着边」更强的信号，
/// 用 `<=` / `>=` 自然覆盖，不需要额外分支。
fn monitor_alert(position: Option<f64>, threshold: f64) -> Option<&'static str> {
    let position = position?;
    if position <= threshold {
        Some("low")
    } else if position >= 1.0 - threshold {
        Some("high")
    } else {
        None
    }
}

/// 报警带常量。写死不进 Query:用页面最严档,做成旋钮只会诱导挑参数。
const TURN_BAND: f64 = 0.03;

/// 回撤档按品种定(DEC-070,**2026-08-18 用干净数据重测后修订,见 DEC-075**)。
///
/// 回撤线画在「位置」刻度上,起作用的波动率是**位置的日间抖动**,不是价格波动率:
/// - JM 抖动全场最高(中位 6.4pp/日,P90 21pp),10% 线只有 1.5 天正常抖动宽,
///   噪音一天就穿——深到 20%(≈3 倍日抖动)才滤得住,逐笔核查躲开三笔大亏;
/// - JD 是唯一「早进不受罚」的品种(早进组中位 +7.4%,季节备货趋势盖过波动),
///   深回撤对它纯粹让利,浅至 5%;
/// - FG-SA 跨品种回归快,逐年留一 4/6 选 8%,深档代价惨重(20% 档 −29.7%);
///   **但它全部档位都是负期望,档位选择的意义有限,这个组合本身就该谨慎**;
/// - **AP 已退回默认档**:郑商所收盘价 0 的脏数据(DEC-073)修掉后重测,
///   8/10/12 三档结果完全相同(+28.9%),深档优势从 14.7pt 缩到 5.1pt,逐年
///   一致性从 8/8 掉到 5/8;而 AP 的位置抖动是全场**最低**的,深档优势从来没有
///   机制解释。撑不住就收回去。
/// - LH 与 FG 跨期相邻档正负翻转、逐年选择不稳,挑了就是过拟合,维持 10%。
///
/// **JM 与 JD 是大商所品种,数据未受那次污染影响,机制与数据两条腿都还在,不动。**
///
/// 与采集 SQL turn_crosses 的分档(compute-spread-monitor.sql)同值,改一处必须
/// 同批改另一处并重跑重算。
fn turn_retreat(instrument_1: &str, instrument_2: &str) -> f64 {
    if instrument_1 != instrument_2 {
        // 目前唯一的跨品种组合是玻璃纯碱;未来若出现别的跨品种对,回默认档。
        return if (instrument_1, instrument_2) == ("FG", "SA") {
            0.08
        } else {
            0.10
        };
    }
    match instrument_1 {
        "JM" => 0.20,
        "JD" => 0.05,
        _ => 0.10,
    }
}

/// 已拐头:近 20 个交易日当年轨曾进报警带,且当前位置已退到带外超过回撤量。
///
/// 「自极值回撤区间的 X%」等价于「位置退 X 个百分点」:报警时价差贴着滚动
/// 极值,极值就是区间端点,(端点 − 当前) / 区间宽 = 1 − 位置。所以不需要另存
/// 极值,只需要近 20 日位置的 max/min(迁移 202608170004)。X 按品种定,见
/// `turn_retreat`。两侧同时满足(20 日内既摸过上带又摸过下带)取**离自家门槛更近**
/// 的一侧 —— 那是刚穿线的、还能进的那一侧(DEC-088)。
fn monitor_turn(
    pos: Option<f64>,
    hi20: Option<f64>,
    lo20: Option<f64>,
    retreat: f64,
) -> Option<&'static str> {
    let pos = pos?;
    let high = hi20.is_some_and(|h| h >= 1.0 - TURN_BAND) && pos <= 1.0 - retreat;
    let low = lo20.is_some_and(|l| l <= TURN_BAND) && pos >= retreat;
    match (high, low) {
        (true, false) => Some("high"),
        (false, true) => Some("low"),
        // 两侧同时成立 = 20 日内既摸过上带又摸过下带。取**刚穿线的那一侧**,
        // 也就是离自己门槛**更近**的一侧(DEC-088,2026-08-19 修正方向)。
        //
        // 原来取的是「离门槛更远」,想的是「退得更多 = 拐得更实」,但退得多恰恰
        // 说明那一侧的回归**已经走完了**,不是能进的场。
        // 实例 LH2611−LH2705 @2026-08-19:位置 0.865,离高位线 0.90 只有 0.035
        // (08-18 还在 1.0,今天刚穿下来),离低位线 0.10 有 0.765(20 日前创的新低,
        // 那波做多价差已经走了 715 点)。原实现报「低位」,把一个走完的机会当成
        // 当前信号,还顺带把统计切到低位侧(持到期 −635)判成不合格 —— 而高位侧
        // 是 5/5、持到期 +635,本该亮 ⚡ 做空。
        (true, true) => Some(if (1.0 - retreat - pos) <= (pos - retreat) {
            "high"
        } else {
            "low"
        }),
        (false, false) => None,
    }
}

/// 报告表「昨持仓」的合成:实际昨行优先;席位整日掉榜但当日各腿增减齐全时,
/// 用「持仓 − 增减」的反推值补上(运营者 2026-08-17 拍板:回榜日能反推的要写进
/// 报告表);两者都没有的席位不计入,一家都没有 → 整格未知(横杠)。
/// 返回 (值, 是否含反推成分)。
///
/// `pub(super)`:它服务的是报告表(在父模块),只是位置上落在监控这一段里。
pub(super) fn report_prev_net(
    yesterday: &[database::spread_analytics::ReportNetRow],
    today: &[database::spread_analytics::ReportNetRow],
    members: &[String],
    instrument: &str,
) -> (Option<String>, bool) {
    let mut total = Decimal::ZERO;
    let mut seen = false;
    let mut inferred_used = false;
    for name in members {
        if let Some(row) = yesterday
            .iter()
            .find(|row| row.instrument == instrument && &row.member == name)
        {
            total += parse_decimal(&row.net_position);
            seen = true;
            continue;
        }
        let inferred = today
            .iter()
            .find(|row| row.instrument == instrument && &row.member == name && row.inferable);
        if let Some(row) = inferred
            && let Some(raw) = row.inferred_prev.as_deref()
        {
            total += parse_decimal(raw);
            seen = true;
            inferred_used = true;
        }
    }
    (seen.then(|| total.normalize().to_string()), inferred_used)
}

/// 拐头是不是今天刚发生:前一日位置还在回撤线的另一侧。
///
/// 不必看前一日的 hi20:band 触碰当天位置必然 ≥0.97 > 0.90,所以「昨天拐头不成立、
/// 今天成立」只可能因为位置今天穿线,不可能因为 band 今天才进窗(自相矛盾)。
/// 判定线附近的抖动(穿线→弹回→再穿线)会再亮一次——没上车的人得到第二次提示,
/// 已上车的人无视即可。
///
/// (这段文档 2026-08-20 之前一直错贴在 `report_prev_net` 头上,本函数自己一句没有
/// ——两个不相干函数的注释黏成了一块。拆模块时编译器报可见性错才顺带发现。)
fn monitor_turn_is_new(turn: Option<&str>, prev_pair: Option<f64>, retreat: f64) -> bool {
    match turn {
        Some("high") => prev_pair.is_some_and(|p| p > 1.0 - retreat),
        Some("low") => prev_pair.is_some_and(|p| p < retreat),
        _ => false,
    }
}

/// 组合窗口止点(先到期腿的散户最后交易日)与当日之间的**剩余交易日**。
///
/// 口径照 5A 的 `last_weekday_before_delivery`:止点=交割月前月最后一个非周末日;
/// 计数按周内日近似(节假日没有价格点,误差 ±2 天以内,红线用途足够)。
/// 《体系》红线:交割前 15 个交易日全部清仓;留一法数据:合格段剩余 <15 日持到底
/// 中位 −21.7%、15~40 日 −32.5%、>40 日 +54.8%(DEC-067)。
fn days_to_window_end(c1: &str, c2: &str, today: Date) -> Option<i32> {
    fn deadline(code: &str) -> Option<Date> {
        let digits = code.find(|c: char| c.is_ascii_digit())?;
        let raw = &code[digits..];
        if raw.len() != 4 {
            return None;
        }
        let year = 2000 + raw[0..2].parse::<i32>().ok()?;
        let month = raw[2..4].parse::<u8>().ok()?;
        let (py, pm) = if month == 1 {
            (year - 1, 12u8)
        } else {
            (year, month - 1)
        };
        let month = time::Month::try_from(pm).ok()?;
        let mut day = time::util::days_in_month(month, py);
        loop {
            let date = Date::from_calendar_date(py, month, day).ok()?;
            if !matches!(
                date.weekday(),
                time::Weekday::Saturday | time::Weekday::Sunday
            ) {
                return Some(date);
            }
            day -= 1;
        }
    }
    let end = deadline(c1)?.min(deadline(c2)?);
    if end <= today {
        return Some(0);
    }
    let mut count = 0i32;
    let mut cursor = today.next_day()?;
    while cursor <= end {
        if !matches!(
            cursor.weekday(),
            time::Weekday::Saturday | time::Weekday::Sunday
        ) {
            count += 1;
        }
        cursor = cursor.next_day()?;
    }
    Some(count)
}

fn track_position(track: &SpreadMonitorTrack) -> Option<f64> {
    track.position.as_deref()?.parse().ok()
}

/// 两条轨合成一个结论，**只看位置与阈值**。
///
/// **方向可能相反**：焦煤 JM2609−JM2701 在 2026-08-11 就是当年高位（95.1%）、
/// 历年低位（16.1%）。这时报「更极端的那条」——离中线更远的一个。随便挑一条
/// 会有一半的机会把方向说反，而页面上看不出它挑错了。
///
/// 写成不依赖 `SpreadMonitorTrack` 的形式，是因为判段首日要拿**前一交易日**的两个
/// 位置走同一条规则。同一个「取更极端那条」的判断在两处各写一遍，迟早会漂。
fn combined_alert_at(
    pair: Option<f64>,
    years: Option<f64>,
    threshold: f64,
) -> Option<&'static str> {
    [pair, years]
        .into_iter()
        .flatten()
        .filter_map(|position| {
            monitor_alert(Some(position), threshold).map(|alert| ((position - 0.5).abs(), alert))
        })
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, alert)| alert)
}

/// 到达概率曲线(DEC-095)。**合并品种与方向**的经验生存函数:
/// `P(能走到 z 个 σ√T 之外)` = 历史上从同样远近的处境出发,在窗口止点前摸到过的比例。
///
/// 为什么合并:逐品种、逐方向的版本**样本外崩了**(焦煤 25%→48%、生猪 69%→52%),
/// 那些差异是那段行情往哪边走了(=漂移),不是品种特性。合并之后按剩余期分桶,
/// 样本外差 ≤2.6 个点(14.4 万个观测)。
///
/// **固化成常量、不每晚重算**:这是研究结论,应当随代码评审一起变,不该因为多了
/// 一天数据就让页面上的数字无声漂移。
///
/// **重估跑 `python research/run_shelf_prob.py`** ——它的第四节既产出可直接粘贴的
/// 常量,也把新算的曲线与这里的现值逐桶对拍。2026-08-20 已验证四个桶最大差 0.00,
/// 即下面这 124 个数完全可复现。(在那之前那句「重估要跑本脚本」是空头支票:脚本
/// 只有三段探索性打印,根本不产出常量——**烤死的数字必须留下能跑通的来路**。)
///
/// **它不知道方向**:上下两侧用同一条曲线。方向由「日线收盘突破平台位」那条规矩定。
/// **逐年离散很大**:z=1.0 长期 42%,最低 17%(2014)、最高 53%(2019)——界面必须写出来。
/// 曲线的样本下界。低于它不给概率——拟合时这些观测就没参与(见 `reach_pct`)。
const REACH_MIN_DAYS: i32 = 5;

const REACH_CURVE: [&[f64]; 4] = [
    // 剩余 5~20 个交易日,样本 18,848
    &[
        84.2, 78.4, 72.8, 67.3, 62.2, 57.3, 52.9, 48.6, 44.6, 40.8, 37.6, 34.7, 32.2, 29.7, 27.6,
        25.5, 23.5, 21.6, 20.1, 18.5, 17.2, 16.0, 15.0, 13.9, 13.0, 12.1, 11.4, 10.7, 9.9, 9.3,
        8.6,
    ],
    // 剩余 21~40 个交易日,样本 23,410
    &[
        91.4, 85.5, 79.9, 74.3, 68.7, 63.6, 59.0, 54.5, 50.5, 46.8, 43.4, 40.1, 36.9, 34.3, 31.8,
        29.6, 27.4, 25.6, 23.7, 22.1, 20.7, 19.2, 17.9, 16.7, 15.5, 14.5, 13.5, 12.6, 11.8, 11.1,
        10.3,
    ],
    // 剩余 41~80 个交易日,样本 43,902
    &[
        95.3, 89.9, 83.5, 77.1, 71.0, 65.4, 60.4, 56.0, 51.9, 47.9, 44.3, 41.1, 37.9, 35.4, 33.1,
        30.9, 28.9, 27.0, 25.3, 23.7, 22.1, 20.6, 19.4, 18.1, 17.1, 15.9, 14.9, 14.0, 13.1, 12.3,
        11.6,
    ],
    // 剩余 >80 个交易日,样本 57,542
    &[
        97.5, 91.9, 84.4, 75.9, 68.2, 61.1, 55.0, 50.0, 45.6, 41.7, 38.3, 35.1, 32.4, 30.1, 28.0,
        26.3, 24.6, 22.9, 21.3, 19.9, 18.5, 17.1, 16.0, 15.0, 14.0, 13.1, 12.3, 11.5, 10.9, 10.2,
        9.5,
    ],
];

/// 把库里存的平台位事实,配上读时才知道的东西:相对现价的偏移、z、到达概率、
/// 以及这一行要做的那笔交易下它是卖点还是止损。
///
/// 角色按 `trade_side` 定(DEC-088 的同一个侧别):做空价差(high)时下方是目标、
/// **上方最近的一档是止损**;做多价差(low)反过来。没有交易侧就不给角色——
/// 那种行本来就没有一笔要做的交易。
fn build_shelves(
    raw: Option<&str>,
    spread: Option<f64>,
    sigma: Option<f64>,
    days_left: Option<i32>,
    trade_side: Option<&str>,
) -> Vec<SpreadShelf> {
    let (Some(raw), Some(spread)) = (raw, spread) else {
        return Vec::new();
    };
    let Ok(items) = serde_json::from_str::<Vec<serde_json::Value>>(raw) else {
        return Vec::new();
    };
    let scale = match (sigma, days_left) {
        (Some(s), Some(d)) if s > 0.0 && d > 0 => Some(s * f64::from(d).sqrt()),
        _ => None,
    };
    let num = |v: &serde_json::Value, k: &str| -> Option<f64> {
        v.get(k).and_then(|x| match x {
            serde_json::Value::Number(n) => n.as_f64(),
            serde_json::Value::String(t) => t.parse().ok(),
            _ => None,
        })
    };
    let mut out: Vec<SpreadShelf> = items
        .iter()
        .filter_map(|v| {
            let level = num(v, "level")?;
            let offset = level - spread;
            let z = scale.map(|s| (offset.abs() / s * 100.0).round() / 100.0);
            Some(SpreadShelf {
                level: format!("{}", level.round() as i64),
                lo: format!("{}", num(v, "lo").unwrap_or(level).round() as i64),
                hi: format!("{}", num(v, "hi").unwrap_or(level).round() as i64),
                touches: v
                    .get("touches")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0),
                offset: format!("{}", offset.round() as i64),
                z: z.map(|x| format!("{x:.2}")),
                reach_pct: z.zip(days_left).and_then(|(x, d)| reach_pct(x, d)),
                role: String::new(),
            })
        })
        .collect();
    // 按档位从高到低,库里已经是这个序,这里不依赖它。
    out.sort_by(|a, b| {
        b.level
            .parse::<i64>()
            .unwrap_or(0)
            .cmp(&a.level.parse::<i64>().unwrap_or(0))
    });
    let Some(side) = trade_side else {
        return out;
    };
    // 目标在交易方向那一侧;止损取**反方向最近的一档**——与运营者的规矩对称:
    // 「日线收盘突破平台位,就会前往下一个平台」,反过来突破就是止损。
    let target_above = side == "low";
    let mut stop_taken = false;
    let iter: Box<dyn Iterator<Item = &mut SpreadShelf>> = if target_above {
        Box::new(out.iter_mut()) // 止损在下方:从高到低,最先遇到的下方档最近
    } else {
        Box::new(out.iter_mut().rev()) // 止损在上方:从低到高
    };
    for sh in iter {
        let off: i64 = sh.offset.parse().unwrap_or(0);
        if off == 0 {
            continue;
        }
        let above = off > 0;
        if above == target_above {
            sh.role = "target".to_string();
        } else if !stop_taken {
            sh.role = "stop".to_string();
            stop_taken = true;
        }
    }
    out
}

fn reach_bucket(days_left: i32) -> usize {
    match days_left {
        ..=20 => 0,
        21..=40 => 1,
        41..=80 => 2,
        _ => 3,
    }
}

/// 曲线上按 0.1 的格子线性插值。z 超出格子就取末端——外推没有依据。
///
/// **剩余不足 5 个交易日一律不给概率**(2026-08-20 审计补):拟合脚本
/// `research/run_shelf_prob.py` 第一步就是 `a[a["T"] >= 5]`,把不足 5 天的
/// 观测**排除在样本之外**。而分桶是 `..=20 => 0`,会把 1~4 天也喂给那条
/// 「5~20 日」的曲线——这正是脚本自己打印的警告:「差很多就必须把剩余期也
/// 当成一维,否则**快到期时会给出虚高的概率**」。
///
/// 每一对合约的剩余天数都会一路走到 0,这个窗口必然经过。同一个函数对 z
/// 明写「外推没有依据」,对天数就不能松。返回 `None`,页面显示「—」并注明
/// 原因,**不拿一个虚高的数字糊上去**。
fn reach_pct(z: f64, days_left: i32) -> Option<f64> {
    if !z.is_finite() || z < 0.0 || days_left < REACH_MIN_DAYS {
        return None;
    }
    let curve = REACH_CURVE[reach_bucket(days_left)];
    let last = curve.len() - 1;
    let x = (z / 0.1).min(last as f64);
    let i = x.floor() as usize;
    let p = if i >= last {
        curve[last]
    } else {
        curve[i] + (curve[i + 1] - curve[i]) * (x - i as f64)
    };
    Some((p * 10.0).round() / 10.0)
}

/// 这一行**要做的那笔交易**在哪一侧 —— 拐头侧优先(DEC-088)。
///
/// ⚡ 进场由拐头触发,所以资格、统计、方向文案都必须锚在拐头侧。原实现是
/// `alert.or(turn)`,两侧相反时会拿报警侧的成绩给拐头侧的交易发通行证。
/// 没拐头只报警的行按报警侧:那是「机会出现、还没到上车点」,本来就没有 ⚡。
fn trade_side(alert: Option<&'static str>, turn: Option<&'static str>) -> Option<&'static str> {
    turn.or(alert)
}

fn combined_alert(
    pair: &SpreadMonitorTrack,
    years: Option<&SpreadMonitorTrack>,
    threshold: f64,
) -> Option<&'static str> {
    combined_alert_at(
        track_position(pair),
        years.and_then(track_position),
        threshold,
    )
}

/// 平台位阶梯里的一档(DEC-095)。
///
/// 档位、区间、触碰回合是**库里存的事实**;偏移、z、到达概率、卖点/止损全部**读时算**
/// ——与报警/拐头/合格同一条纪律(存事实不存结论)。
#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct SpreadShelf {
    /// 档位(该档并入的转折位均值)。
    pub level: String,
    /// 并档区间的两端。链式合并会让几个转折位并出一个跨几十点的档,
    /// 只报均值是假精度,所以两端一起给出来。
    pub lo: String,
    pub hi: String,
    /// 收盘落在该档 ±25 点内的**独立回合数**(连续日算一回合)。
    pub touches: i64,
    /// 相对现价差的点数。**正 = 在上方**。
    pub offset: String,
    /// 距离 ÷ (σ√剩余交易日)。没有 σ 或剩余天数时为空。
    pub z: Option<String>,
    /// 到达概率(%),来自固化的合并曲线。**不含方向判断**,逐年离散很大。
    pub reach_pct: Option<f64>,
    /// `"target"`(卖点侧)/ `"stop"`(反方向最近的一档)/ `""`。
    /// 按这一行要做的那笔交易定:做空价差时下方是目标、上方最近的一档是止损。
    pub role: String,
}

/// 组装同侧的历年统计。样本为 0(或整块缺失)时返回 None —— 界面不显示这一块，
/// 而不是显示一个「0% 回归率」：那看着像结论，其实是没有数据。
#[allow(clippy::too_many_arguments)]
fn revert_stats(
    side: &str,
    hit: Option<i32>,
    n: Option<i32>,
    move_points: Option<String>,
    drift_points: Option<String>,
    mae_points: Option<String>,
    mae_max_points: Option<String>,
    days: Option<i32>,
) -> Option<SpreadRevertStats> {
    let (hit, n) = (hit?, n?);
    if n <= 0 {
        return None;
    }
    Some(SpreadRevertStats {
        side: side.to_string(),
        hit,
        n,
        rate: format!("{:.4}", f64::from(hit) / f64::from(n)),
        move_points,
        drift_points,
        mae_points,
        mae_max_points,
        days,
    })
}

fn monitor_track(
    low: Option<String>,
    high: Option<String>,
    position: Option<String>,
    days: Option<i32>,
    threshold: f64,
) -> Option<SpreadMonitorTrack> {
    let (low, high) = (low?, high?);
    let alert = monitor_alert(
        position.as_deref().and_then(|value| value.parse().ok()),
        threshold,
    );
    Some(SpreadMonitorTrack {
        low,
        high,
        position,
        days,
        alert: alert.map(str::to_string),
    })
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/monitor",
    params(
        ("threshold" = Option<f64>, Query),
        ("trade_date" = Option<String>, Query),
        ("history" = Option<bool>, Query)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SpreadMonitorResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn query_spread_monitor(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<SpreadMonitorQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;

    let threshold = query.threshold.unwrap_or(MONITOR_THRESHOLD_DEFAULT);
    if !threshold.is_finite() || threshold <= 0.0 || threshold > MONITOR_THRESHOLD_MAX {
        return Err(SpreadApiError::Validation("invalid_threshold", request_id));
    }

    let trade_date = match query.trade_date.as_deref().map(str::trim) {
        None | Some("") => None,
        Some(value) => Some(
            Date::parse(value, &time::format_description::well_known::Iso8601::DATE)
                .map_err(|_| SpreadApiError::Validation("invalid_trade_date", request_id))?,
        ),
    };

    // 历史模式(DEC-070):一次取回全部快照日,前端只渲染进场行——
    // 运营者要看历年 ⚡ 不必逐日期点选。判定与单日路径同一套读时逻辑。
    let rows = if query.history.unwrap_or(false) {
        database::spread_analytics::spread_monitor_history(&state.auth.pool, context.workspace_id())
            .await
    } else {
        match trade_date {
            Some(day) => {
                database::spread_analytics::spread_monitor_on(
                    &state.auth.pool,
                    context.workspace_id(),
                    day,
                )
                .await
            }
            None => {
                database::spread_analytics::spread_monitor_snapshot(
                    &state.auth.pool,
                    context.workspace_id(),
                    MONITOR_STALE_DAYS,
                )
                .await
            }
        }
    }
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let dates = database::spread_analytics::spread_monitor_dates(
        &state.auth.pool,
        context.workspace_id(),
        400,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let notes =
        database::spread_analytics::load_template_notes(&state.auth.pool, context.workspace_id())
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;

    // 现货基差背景(DEC-074)。按最新快照日取一批,历史模式下不逐日取——
    // 历史行看的是当时的进场信号,基差是"现在的产业背景",给最新的即可。
    let basis_rows = match rows.iter().map(|row| row.trade_date).max() {
        Some(day) => database::spread_analytics::load_spot_basis(
            &state.auth.pool,
            context.workspace_id(),
            day,
        )
        .await
        .unwrap_or_default(),
        None => Vec::new(),
    };
    let basis_map: std::collections::HashMap<String, SpreadBasisInfo> = basis_rows
        .into_iter()
        .map(|row| {
            (
                row.instrument.clone(),
                SpreadBasisInfo {
                    instrument: row.instrument,
                    trade_date: row.trade_date.to_string(),
                    spot_price: row.spot_price,
                    dominant_basis: row.dominant_basis,
                    dominant_basis_rate: row.dominant_basis_rate,
                    percentile: row.basis_percentile,
                },
            )
        })
        .collect();
    let note_map: std::collections::HashMap<(String, i32, String, i32), String> = notes
        .into_iter()
        .map(|(i1, m1, i2, m2, note)| ((i1, m1, i2, m2), note))
        .collect();
    let month_of = |contract: &str| -> Option<i32> {
        contract
            .get(contract.len().saturating_sub(2)..)
            .and_then(|mm| mm.parse::<i32>().ok())
    };

    // 过期判定的基准日 = 这批行里最新的快照日(不是墙钟,结果可复现)。
    let latest_snapshot = rows.iter().map(|row| row.trade_date).max();

    let items: Vec<SpreadMonitorItem> = rows
        .into_iter()
        .map(|row| {
            let note = match (month_of(&row.contract_1), month_of(&row.contract_2)) {
                (Some(m1), Some(m2)) => note_map
                    .get(&(row.instrument_1.clone(), m1, row.instrument_2.clone(), m2))
                    .cloned(),
                _ => None,
            };
            let pair = monitor_track(
                Some(row.pair_low),
                Some(row.pair_high),
                row.pair_position,
                Some(row.pair_days),
                threshold,
            )
            .expect("当年轨的上下界在库里是 not null");
            let years = monitor_track(
                row.years_low,
                row.years_high,
                row.years_position,
                row.years_days,
                threshold,
            );
            let alert = combined_alert(&pair, years.as_ref(), threshold);

            // 段首日：今天触发、前一交易日按同一阈值不触发。前一日位置整个缺失时
            // `combined_alert_at` 返回 None，会把「判不了」误判成「刚触发」，所以
            // 额外要求至少有一条轨的前值存在。
            let parse = |value: &Option<String>| -> Option<f64> {
                value.as_deref().and_then(|raw| raw.parse().ok())
            };
            let prev_pair = parse(&row.prev_pair_position);
            let prev_years = parse(&row.prev_years_position);
            let has_prev = prev_pair.is_some() || prev_years.is_some();
            let is_new_alert = alert.is_some()
                && has_prev
                && combined_alert_at(prev_pair, prev_years, threshold).is_none();

            let expired = latest_snapshot.is_some_and(|day| {
                days_to_window_end(&row.contract_1, &row.contract_2, day) == Some(0)
            });
            // 跨品种组合按第一条腿的品种给基差(玻纯以玻璃为准):两个品种各有
            // 自己的现货,挑一个是显示取舍,不是计算。
            let basis = basis_map
                .get(&row.instrument_1)
                .map(|info| SpreadBasisInfo {
                    instrument: info.instrument.clone(),
                    trade_date: info.trade_date.clone(),
                    spot_price: info.spot_price.clone(),
                    dominant_basis: info.dominant_basis.clone(),
                    dominant_basis_rate: info.dominant_basis_rate.clone(),
                    percentile: info.percentile.clone(),
                });
            let retreat = turn_retreat(&row.instrument_1, &row.instrument_2);
            let turn = monitor_turn(
                track_position(&pair),
                parse(&row.pair_pos_hi20),
                parse(&row.pair_pos_lo20),
                retreat,
            );
            let days_left = days_to_window_end(&row.contract_1, &row.contract_2, row.trade_date);
            let is_new_turn = monitor_turn_is_new(turn, prev_pair, retreat);
            let turn_crosses = match turn {
                Some("high") => row.turn_crosses_high_20,
                Some("low") => row.turn_crosses_low_20,
                _ => None,
            };

            // 计数是 Copy、点数是短字符串 clone 一下，都不会妨碍下面把 row 的其余
            // 字段移走。统计与阈值无关，所以这里不再挑档位。
            let stats_for = |side: &'static str| {
                if side == "high" {
                    revert_stats(
                        side,
                        row.revert_high_hit,
                        row.revert_high_n,
                        row.revert_high_move.clone(),
                        row.revert_high_drift.clone(),
                        row.revert_high_mae.clone(),
                        row.revert_high_mae_max.clone(),
                        row.revert_high_days,
                    )
                } else {
                    revert_stats(
                        side,
                        row.revert_low_hit,
                        row.revert_low_n,
                        row.revert_low_move.clone(),
                        row.revert_low_drift.clone(),
                        row.revert_low_mae.clone(),
                        row.revert_low_mae_max.clone(),
                        row.revert_low_days,
                    )
                }
            };

            // **拐头侧优先**(DEC-088,2026-08-19 修 BUG)。原来是 `alert.or(turn)`,
            // 想的是「报警侧更贴近当下」;但 ⚡ 进场是**拐头**触发的,资格却拿报警侧
            // 的统计去判——两侧相反时,合格标说的是 A 方向,进场标说的是 B 方向,
            // 乘在一起就放行了一笔没有任何统计支持的交易。
            //
            // 实例 JM2612−JM2705 @2026-08-06:历年轨 3.6% 报低位、当年轨自 100% 退到
            // 70.8% 拐头报高位。显示的是低位侧(13/13、持到期 +45,合格),⚡ 指的却是
            // 高位侧(做空),而高位侧持到期 −45 本该判不合格。此后两周价差从 −155 走到
            // −71.5,涨 83.5 点——做空方向反了。
            //
            // 没拐头只报警的行仍按报警侧给统计:那是「机会出现、还没到上车点」,
            // 本来就不该有 ⚡,给统计是为了让人提前看数字。
            let trade_side = trade_side(alert, turn);
            let revert = trade_side.and_then(stats_for);
            // 另一侧只在**两侧方向相反**时给:这正是上面那个 BUG 的现场,藏起来就是
            // 藏证据。方向一致时给它只会让页面多出一串同义数字。
            let shelves = build_shelves(
                row.shelves.as_deref(),
                row.spread.parse::<f64>().ok(),
                row.spread_sigma
                    .as_deref()
                    .and_then(|v| v.parse::<f64>().ok()),
                days_left,
                trade_side,
            );
            let revert_alt = match (alert, turn) {
                (Some(a), Some(t)) if a != t => stats_for(a),
                _ => None,
            };

            SpreadMonitorItem {
                trade_date: row.trade_date.to_string(),
                instrument_1: row.instrument_1,
                contract_1: row.contract_1,
                instrument_2: row.instrument_2,
                contract_2: row.contract_2,
                is_cross_variety: row.is_cross_variety,
                spread: row.spread,
                pair,
                years,
                alert: alert.map(str::to_string),
                is_new_alert,
                revert,
                revert_alt,
                shelves,
                turn: turn.map(str::to_string),
                is_new_turn,
                turn_crosses,
                days_left,
                note,
                expired,
                basis,
            }
        })
        .collect();

    // as_of 在过滤前算:历史模式下 items 只剩进场候选行,拿它的 max 会把
    // 「最新快照」显示成最后一次进场的日子。
    let as_of = items.iter().map(|item| item.trade_date.clone()).max();

    // 历史视图只回传进场候选行(今天刚穿线且是本轮首次):快照回填到组合整个
    // 生命周期后全量行有上万,整包返回太重。资格与红线的最终判定仍在前端
    // isEntry 一处,这里只做粗筛,不产生第二份口径。
    let items: Vec<SpreadMonitorItem> = if query.history.unwrap_or(false) {
        items
            .into_iter()
            .filter(|item| item.is_new_turn && item.turn_crosses == Some(1))
            .collect()
    } else {
        items
    };

    Ok(Json(ApiResponse::new(
        SpreadMonitorResponse {
            threshold: threshold.to_string(),
            as_of,
            available_dates: dates.iter().map(ToString::to_string).collect(),
            items,
        },
        request_id,
    ))
    .into_response())
}

#[cfg(test)]
mod monitor_tests {
    use super::*;

    fn track(position: f64, threshold: f64) -> SpreadMonitorTrack {
        SpreadMonitorTrack {
            low: "0".into(),
            high: "1".into(),
            position: Some(position.to_string()),
            days: Some(200),
            alert: monitor_alert(Some(position), threshold).map(str::to_string),
        }
    }

    #[test]
    fn the_threshold_is_applied_at_read_time_not_baked_in() {
        // 同一个位置，阈值不同结论不同——这正是「存位置不存结论」换来的自由度。
        assert_eq!(monitor_alert(Some(0.85), 0.10), None);
        assert_eq!(monitor_alert(Some(0.85), 0.20), Some("high"));
        assert_eq!(monitor_alert(Some(0.15), 0.10), None);
        assert_eq!(monitor_alert(Some(0.15), 0.20), Some("low"));
    }

    #[test]
    fn a_position_outside_the_band_is_a_stronger_alert_not_a_missing_one() {
        // 历年轨用的是第 2.5 / 97.5 百分位，当前价差可以落在区间之外：位置为负或
        // 大于 1。那比「贴着边」更极端，绝不能因为不在 [0,1] 里就漏报。
        assert_eq!(monitor_alert(Some(-0.014), 0.10), Some("low"));
        assert_eq!(monitor_alert(Some(1.332), 0.10), Some("high"));
    }

    #[test]
    fn when_the_two_tracks_disagree_the_more_extreme_one_wins() {
        // 生产实例：焦煤 JM2609−JM2701 在 2026-08-11 是当年 95.1%、历年 16.1%。
        // 在 20% 阈值下两条都触发且方向相反；离中线的距离 0.451 对 0.339，
        // 所以报「当年高位」。
        //
        // 这条测试存在的理由：原来那版用 max_by 配了个恒等比较器，等于随便挑第一条，
        // 有一半机会把方向说反——而页面上看不出它挑错了。
        let pair = track(0.951, 0.20);
        let years = track(0.161, 0.20);
        assert_eq!(pair.alert.as_deref(), Some("high"));
        assert_eq!(years.alert.as_deref(), Some("low"));
        assert_eq!(combined_alert(&pair, Some(&years), 0.20), Some("high"));

        // 同一组数据在默认的 10% 阈值下只有当年那条触发——16.1% 够不着 10% 的低位带。
        // 写在这里是为了记住：设计图里那些「历年低位」的说法用的是 20% 阈值。
        let pair_10 = track(0.951, 0.10);
        let years_10 = track(0.161, 0.10);
        assert_eq!(years_10.alert, None);
        assert_eq!(
            combined_alert(&pair_10, Some(&years_10), 0.10),
            Some("high")
        );

        // 反过来也要对：历年更极端时报历年那条。
        let pair_mild = track(0.88, 0.15);
        let years_wild = track(-0.20, 0.15);
        assert_eq!(
            combined_alert(&pair_mild, Some(&years_wild), 0.15),
            Some("low")
        );
    }

    #[test]
    fn the_shelf_ladder_marks_the_stop_on_the_far_side() {
        // LH2611−LH2705 @2026-08-19 的真实档位(库里跑出来的),现价差 −935。
        // 做空价差(high):下方全是目标,**上方最近的一档 −885 是止损**。
        let raw = r#"[
            {"level":-885,"lo":-885,"hi":-885,"touches":2},
            {"level":-1117,"lo":-1155,"hi":-1080,"touches":3},
            {"level":-1355,"lo":-1355,"hi":-1355,"touches":3},
            {"level":-1640,"lo":-1640,"hi":-1640,"touches":1}
        ]"#;
        let out = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), Some("high"));
        assert_eq!(out.len(), 4);
        assert_eq!(out[0].level, "-885");
        assert_eq!(out[0].offset, "50");
        assert_eq!(out[0].role, "stop");
        // 下方三档都是卖点侧,且离得越远概率越低。
        assert_eq!(out[1].role, "target");
        assert_eq!(out[3].role, "target");
        let p1 = out[1].reach_pct.expect("有 σ 与剩余天数就该有概率");
        let p3 = out[3].reach_pct.expect("同上");
        assert!(p1 > p3, "越远的档概率必须越低:{p1} vs {p3}");
        // 做多价差时角色整个翻过来:上方是目标,下方最近的一档是止损。
        let up = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), Some("low"));
        assert_eq!(up[0].role, "target");
        assert_eq!(up[1].role, "stop");
        assert_eq!(up[2].role, "");
    }

    #[test]
    fn shelves_survive_a_missing_sigma_and_an_old_row() {
        // 没有 σ 就没有 z 和概率,但档位与触碰次数照给——它们是库里的事实。
        let raw = r#"[{"level":-1355,"lo":-1355,"hi":-1355,"touches":3}]"#;
        let out = build_shelves(Some(raw), Some(-935.0), None, Some(52), Some("high"));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].touches, 3);
        assert!(out[0].z.is_none() && out[0].reach_pct.is_none());
        // 旧行(这一列还没算过)与坏 JSON 都给空数组,不 panic。
        assert!(build_shelves(None, Some(-935.0), Some(94.4), Some(52), Some("high")).is_empty());
        assert!(build_shelves(Some("坏的"), Some(-935.0), Some(94.4), Some(52), None).is_empty());
        // 没有交易侧就不派角色——那种行没有一笔要做的交易。
        let no_side = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), None);
        assert_eq!(no_side[0].role, "");
    }

    #[test]
    fn the_reach_curve_falls_with_distance_and_never_extrapolates() {
        for days in [10, 30, 60, 120] {
            let mut prev = 101.0;
            for step in 0..=40 {
                let z = f64::from(step) * 0.1;
                let p = reach_pct(z, days).expect("z ≥ 0 一定有值");
                assert!(p <= prev, "剩余 {days} 日、z={z} 处概率不单调:{p} > {prev}");
                prev = p;
            }
            // 超出格子取末端,不外推——外推没有依据。
            assert_eq!(reach_pct(3.0, days), reach_pct(9.9, days));
        }
        assert!(reach_pct(-0.1, 30).is_none());

        // **天数也不许外推**:曲线的样本是 T ≥ 5,1~4 天从来没参与过拟合。
        // 每一对合约都会一路走到 0 天,这不是理论上的边界。
        for days in 0..REACH_MIN_DAYS {
            assert!(
                reach_pct(0.5, days).is_none(),
                "剩余 {days} 日不该给概率——那是拿 5~20 日的曲线外推"
            );
        }
        assert!(reach_pct(0.5, REACH_MIN_DAYS).is_some(), "刚好 5 天要有值");
    }

    #[test]
    fn the_traded_side_is_the_turn_side_not_the_alert_side() {
        // JM2612−JM2705 @2026-08-06 的真实形态:历年轨 3.6% 报**低位**,
        // 当年轨自 100% 退到 70.8% 拐**高位**。⚡ 是拐头给的,统计就得给高位侧
        // ——高位侧持到期 −45,判不合格,⚡ 该灭。原实现给低位侧(持到期 +45)
        // 直接放行了一笔做空,而其后两周价差涨了 83.5 点。
        assert_eq!(trade_side(Some("low"), Some("high")), Some("high"));
        assert_eq!(trade_side(Some("high"), Some("low")), Some("low"));
        // 两侧一致时谁优先都一样
        assert_eq!(trade_side(Some("high"), Some("high")), Some("high"));
        // 只报警没拐头:机会出现但没到上车点,按报警侧给数字,不会有 ⚡
        assert_eq!(trade_side(Some("low"), None), Some("low"));
        // 只拐头没报警:拐头行多半已退出报警带,这正是该看数字的时候
        assert_eq!(trade_side(None, Some("high")), Some("high"));
        assert_eq!(trade_side(None, None), None);
    }

    #[test]
    fn no_alert_anywhere_means_no_alert() {
        let pair = track(0.5, 0.10);
        let years = track(0.42, 0.10);
        assert_eq!(combined_alert(&pair, Some(&years), 0.10), None);
        assert_eq!(combined_alert(&pair, None, 0.10), None);
    }

    #[test]
    fn a_combination_without_a_years_track_still_works() {
        // 历年轨可能缺席：跨品种组合的历史年份不够，或该月份组合是头一年出现。
        // 缺席不该让整行消失。
        let pair = track(0.97, 0.10);
        assert_eq!(combined_alert(&pair, None, 0.10), Some("high"));
    }

    /// 段首日判定抽出来复算一遍：与 handler 里那段是同一条规则。
    fn new_alert(today: (f64, Option<f64>), prev: (Option<f64>, Option<f64>), thr: f64) -> bool {
        let alert = combined_alert_at(Some(today.0), today.1, thr);
        let has_prev = prev.0.is_some() || prev.1.is_some();
        alert.is_some() && has_prev && combined_alert_at(prev.0, prev.1, thr).is_none()
    }

    #[test]
    fn a_new_alert_is_one_that_was_not_there_yesterday() {
        // 昨天在区间中部、今天贴到下沿 —— 这才是「新出现的机会」。
        assert!(new_alert((0.01, None), (Some(0.30), None), 0.03));
        // 昨天已经在极值里，今天还在 —— 持续触发，不是新的。焦煤 2026 年 64% 的
        // 交易日都在触发，全靠这一条把长段压下去。
        assert!(!new_alert((0.01, None), (Some(0.02), None), 0.03));
        // 今天没触发，昨天触没触发都无所谓。
        assert!(!new_alert((0.50, None), (Some(0.01), None), 0.03));
    }

    #[test]
    fn without_yesterdays_position_nothing_is_marked_new() {
        // 该组合的第一天、或前一日没有快照。判不了就不打标记：把「不知道」当成
        // 「刚触发」，页面上会天天冒出假的新触发，而且看不出是假的。
        assert!(!new_alert((0.01, None), (None, None), 0.03));
    }

    #[test]
    fn the_segment_start_follows_the_same_two_track_rule() {
        // 前一日两轨方向相反时，也要按「更极端那条」判，否则会出现今天用合成轨、
        // 昨天用当年轨的错配。昨天当年轨 0.951(高位触发)、历年 0.161(低位触发)，
        // 20% 阈值下昨天已经触发，所以今天不是段首日。
        assert!(!new_alert(
            (0.97, Some(0.5)),
            (Some(0.951), Some(0.161)),
            0.20
        ));
        // 同一组前值放到 3% 阈值下，两条都够不着 —— 今天才算新触发。
        assert!(new_alert(
            (0.01, Some(0.5)),
            (Some(0.951), Some(0.161)),
            0.03
        ));
    }

    #[test]
    fn the_revert_stats_carry_all_three_numbers() {
        // 生产实例：JD2609/JD2701 在 2026-08-14 的高位统计。
        let s = revert_stats(
            "high",
            Some(11),
            Some(12),
            Some("101.5".into()),
            Some("81".into()),
            Some("62".into()),
            Some("247".into()),
            Some(12),
        )
        .expect("有样本");
        assert_eq!((s.hit, s.n), (11, 12));
        assert_eq!(s.rate, "0.9167");
        assert_eq!(s.move_points.as_deref(), Some("101.5"));
        assert_eq!(s.drift_points.as_deref(), Some("81"));
        assert_eq!(s.mae_points.as_deref(), Some("62"));
        assert_eq!(s.mae_max_points.as_deref(), Some("247"));
        assert_eq!(s.days, Some(12));
    }

    #[test]
    fn without_samples_nothing_is_shown_rather_than_zero_percent() {
        // 样本为 0 显示成「0% 回归率」是最坏的一种错：看着像结论，其实是没有数据。
        assert!(revert_stats("high", None, None, None, None, None, None, None).is_none());
        assert!(revert_stats("high", Some(0), Some(0), None, None, None, None, None).is_none());
        // 计数在、点数缺（中位算不出来）时仍然给比率，只是点数留空。
        let s = revert_stats("low", Some(4), Some(5), None, None, None, None, Some(30))
            .expect("有样本");
        assert_eq!(s.rate, "0.8000");
        assert!(s.move_points.is_none());
    }

    #[test]
    fn a_turn_needs_a_recent_alert_and_a_real_retreat() {
        // 近 20 日进过高位带(hi20=1.0),当前退到 0.88 —— 已拐头(默认档 10%)。
        assert_eq!(
            monitor_turn(Some(0.88), Some(1.0), Some(0.40), 0.10),
            Some("high")
        );
        // 还贴在带里(0.98):机会在,但还没拐头。
        assert_eq!(monitor_turn(Some(0.98), Some(1.0), Some(0.40), 0.10), None);
        // 退了但不够(0.93 > 0.90):不算。
        assert_eq!(monitor_turn(Some(0.93), Some(1.0), Some(0.40), 0.10), None);
        // 报警是 20 多天前的事(hi20 已滑出带外):状态自动过期。
        assert_eq!(monitor_turn(Some(0.85), Some(0.94), Some(0.40), 0.10), None);
        // 低位对称。
        assert_eq!(
            monitor_turn(Some(0.12), Some(0.60), Some(0.01), 0.10),
            Some("low")
        );
        // 位置缺失判不了。
        assert_eq!(monitor_turn(None, Some(1.0), Some(0.0), 0.10), None);
    }

    #[test]
    fn the_retreat_line_is_per_variety() {
        // DEC-070:JM 抖动全场最高要深线,JD 早进不受罚要浅线。
        assert_eq!(turn_retreat("JM", "JM"), 0.20);
        assert_eq!(turn_retreat("JD", "JD"), 0.05);
        assert_eq!(turn_retreat("FG", "SA"), 0.08);
        assert_eq!(turn_retreat("LH", "LH"), 0.10);
        assert_eq!(turn_retreat("FG", "FG"), 0.10);
        // DEC-075:AP 曾按脏数据判成 20%,清洗后 8/10/12 三档同值、逐年一致性
        // 掉到 5/8,且 AP 抖动全场最低没有机制支撑——已退回默认档。别再调深。
        assert_eq!(turn_retreat("AP", "AP"), 0.10);
        // JM 退到 0.88 在默认档算拐头,在自家 20% 档还不算——同样的位置,
        // 不同品种结论不同,这正是分档的意义。
        assert_eq!(
            monitor_turn(Some(0.88), Some(1.0), None, turn_retreat("JM", "JM")),
            None
        );
        assert_eq!(
            monitor_turn(Some(0.79), Some(1.0), None, turn_retreat("JM", "JM")),
            Some("high")
        );
        // JD 5% 档:退过 0.95 就算。
        assert_eq!(
            monitor_turn(Some(0.94), Some(1.0), None, turn_retreat("JD", "JD")),
            Some("high")
        );
    }

    fn net_row(
        member: &str,
        instrument: &str,
        net: &str,
        inferred: Option<&str>,
        inferable: bool,
    ) -> database::spread_analytics::ReportNetRow {
        database::spread_analytics::ReportNetRow {
            member: member.to_string(),
            instrument: instrument.to_string(),
            net_position: net.to_string(),
            inferred_prev: inferred.map(str::to_string),
            inferable,
        }
    }

    #[test]
    fn a_reboard_day_backfills_yesterday_from_todays_change() {
        // 高盛 2026-08-17 实例:前一日白银掉榜(昨无行),今天回榜空 2364、增 14
        // → 反推昨净仓 −2350,并打「推」标。
        let today = vec![net_row("高盛期货", "AG", "-2364", Some("-2350"), true)];
        let (value, inferred) = report_prev_net(&[], &today, &["高盛期货".to_string()], "AG");
        assert_eq!(value.as_deref(), Some("-2350"));
        assert!(inferred);
    }

    #[test]
    fn a_missing_change_poisons_the_inference() {
        // 任何一条腿的增减缺失,反推作废 —— 显示横杠,不显示半截和。
        let today = vec![net_row("高盛期货", "AG", "-2364", Some("-2350"), false)];
        let (value, inferred) = report_prev_net(&[], &today, &["高盛期货".to_string()], "AG");
        assert_eq!(value, None);
        assert!(!inferred);
    }

    #[test]
    fn an_actual_yesterday_row_beats_the_inference() {
        // 昨天真在榜上就用真数,反推只补缺口,不覆盖事实。
        let yesterday = vec![net_row("中信期货", "AG", "-26944", None, false)];
        let today = vec![net_row("中信期货", "AG", "-25222", Some("-99999"), true)];
        let (value, inferred) =
            report_prev_net(&yesterday, &today, &["中信期货".to_string()], "AG");
        assert_eq!(value.as_deref(), Some("-26944"));
        assert!(!inferred);
    }

    #[test]
    fn a_group_mixes_actual_and_inferred_and_flags_it() {
        // 机构合计行:六家有真数、高盛靠反推 —— 合计给和,并因含反推成分打标。
        let yesterday = vec![net_row("中财期货", "AG", "11234", None, false)];
        let today = vec![
            net_row("中财期货", "AG", "11330", Some("11200"), true),
            net_row("高盛期货", "AG", "-2364", Some("-2350"), true),
        ];
        let members = vec!["中财期货".to_string(), "高盛期货".to_string()];
        let (value, inferred) = report_prev_net(&yesterday, &today, &members, "AG");
        assert_eq!(value.as_deref(), Some("8884")); // 11234 + (−2350)
        assert!(inferred);
        // 完全没数的品种照旧未知。
        let (none, flag) = report_prev_net(&yesterday, &today, &members, "AU");
        assert_eq!(none, None);
        assert!(!flag);
    }

    #[test]
    fn the_delivery_red_line_counts_weekdays_to_the_window_end() {
        fn day(y: i32, m: u8, d: u8) -> Date {
            Date::from_calendar_date(y, time::Month::try_from(m).unwrap(), d).unwrap()
        }
        // JD2609/JD2701:先到期腿 09 月,止点 = 2026-08-31(周一)。
        // 从 08-17(周一)数,剩 8/18~8/31 的十个周内日。
        assert_eq!(
            days_to_window_end("JD2609", "JD2701", day(2026, 8, 17)),
            Some(10)
        );
        // FG2701/SA2701:止点 = 2026-12-31(周四),远月组合不在红线附近。
        let left = days_to_window_end("FG2701", "SA2701", day(2026, 8, 17)).unwrap();
        assert!(left > 90, "{left}");
        // 已过止点:0,不给负数。
        assert_eq!(
            days_to_window_end("JD2608", "JD2702", day(2026, 8, 17)),
            Some(0)
        );
        // 解析不了的代码判不了,不硬编。
        assert_eq!(days_to_window_end("JD26", "JD2701", day(2026, 8, 17)), None);
    }

    #[test]
    fn the_entry_day_is_the_day_the_position_crosses_the_line() {
        // FG2701/SA2701 生产序列:08-04 位置 1.000(带内,未拐头)→ 08-05 退到
        // 0.884(拐头,前一日 1.000 在线上方)= 进场日。
        assert!(monitor_turn_is_new(Some("high"), Some(1.0), 0.10));
        // 08-10:前一日 0.855 已在线下 —— 拐头持续中,不再是进场日。
        assert!(!monitor_turn_is_new(Some("high"), Some(0.855), 0.10));
        // 08-07 的抖动:前一日 0.906 弹回线上,再穿线 —— is_new_turn 会再真,
        // 但 ⚡ 由前端按 turn_crosses==1 只认首次(DEC-070,运营者拍板)。
        assert!(monitor_turn_is_new(Some("high"), Some(0.906), 0.10));
        // 没拐头就谈不上进场日;前一日缺失判不了,宁可漏标。
        assert!(!monitor_turn_is_new(None, Some(1.0), 0.10));
        assert!(!monitor_turn_is_new(Some("high"), None, 0.10));
        // 低位对称:前一日 0.05 在线下方,今天 ≥0.10 穿上来。
        assert!(monitor_turn_is_new(Some("low"), Some(0.05), 0.10));
        assert!(!monitor_turn_is_new(Some("low"), Some(0.15), 0.10));
    }

    #[test]
    fn a_crash_through_both_bands_picks_the_side_that_just_crossed() {
        // 20 日内从上带砸到 0.05:低位侧还没退够(0.05 < 0.10),只有高位侧成立。
        assert_eq!(
            monitor_turn(Some(0.05), Some(1.0), Some(0.05), 0.10),
            Some("high")
        );
        // 停在正中央,两侧余量相等 —— 也要有确定的答案,不许随机。
        assert_eq!(
            monitor_turn(Some(0.50), Some(1.0), Some(0.0), 0.10),
            Some("high")
        );
        // LH2611−LH2705 @2026-08-19 的真实形态:位置 0.865,20 日内两带都摸过。
        // 离高位线 0.035(昨天还在 1.0,今天刚穿下来)、离低位线 0.765(那波做多
        // 已经走完 715 点)—— 报**高位**。取「更远」会报低位,把走完的机会当信号。
        assert_eq!(
            monitor_turn(Some(0.865), Some(1.0), Some(0.0), 0.10),
            Some("high")
        );
        // 镜像:刚从底部弹上来一点,该报低位。
        assert_eq!(
            monitor_turn(Some(0.135), Some(1.0), Some(0.0), 0.10),
            Some("low")
        );
    }

    #[test]
    fn a_high_hit_rate_with_a_negative_drift_is_the_trap_worth_showing() {
        // JD2612/JD2701：12 年全都曾跌破起点（rate=100%），但一直持到窗口止点的净
        // 变化中位是 −166 点 —— 方向是反的。只显示 rate 会把这种组合读成安全机会，
        // 所以 drift 必须和 rate 一起出现在响应里。
        let s = revert_stats(
            "high",
            Some(12),
            Some(12),
            Some("88".into()),
            Some("-166".into()),
            None,
            None,
            Some(70),
        )
        .expect("有样本");
        assert_eq!(s.rate, "1.0000");
        assert_eq!(s.drift_points.as_deref(), Some("-166"));
    }
}
