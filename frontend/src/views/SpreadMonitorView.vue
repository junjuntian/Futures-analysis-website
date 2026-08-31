<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { bounceHint, pickBounceDay, type BounceDay, type BounceState } from '../bounce-hint'
import { rollPressureHint, type RollPressureState } from '../roll-pressure-hint'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  getSeatNetPosition,
  type MemberLeg,
  getSpreadMonitor,
  getSpreadVarieties,
  saveSpreadTemplateNote,
  type SpreadMonitorItem,
  type SpreadMonitorTrack,
  type SpreadShelf
} from '../api'
import {
  driftTone,
  isChoppy,
  isDecayZone,
  isQualified,
  isRedLine,
  points,
  revertPct,
  tradeDirection
} from '../revert'
import { edgeOf, offsetText, reachUnavailable, shelfLabel } from '../shelf'
import { useAuthStore } from '../stores/auth'

// 阈值：落在区间两端多少算触发。括号里是 2026-08-11 生产快照上的真实触发数（共 91 组），
// 后端 MONITOR_THRESHOLD_DEFAULT 有完整的量测表与选 5% 的理由。
const THRESHOLDS = [
  { value: 0.03, label: '3% · 最严' },
  { value: 0.05, label: '5% · 默认（约 25 组）' },
  { value: 0.1, label: '10% · 报得多（约 47 组）' },
  { value: 0.2, label: '20% · 几乎全报（约 61 组）' }
]

const threshold = ref(0.05)
const tradeDate = ref('')
const direction = ref<'all' | 'high' | 'low'>('all')
const varietyFilter = ref<string[]>([])
const showQuiet = ref(false)
// 只看刚进极值的。焦煤 2026 年有 64% 的交易日都在 3% 触发（价差持续创新低，滚动
// 区间天天被刷新），而连续触发段的中位长度只有 3 日——长段拖着不放才是噪音的来源。
const onlyNew = ref(false)
// 只看今天的进场信号:每天盘后勾上它,列表要么是空的,要么就是今天该动手的单子。
const onlyEntry = ref(false)
// 历史信号(DEC-070):一次取回全部快照日,只列历来的 ⚡ 进场行——
// 运营者要回看信号不必逐个日期点选。
const historyMode = ref(false)

const items = ref<SpreadMonitorItem[]>([])
const asOf = ref<string | null>(null)
const availableDates = ref<string[]>([])
const loading = ref(false)

// 品种中文名取自 product_instrument_scope，与套利页、席位页同一个来源——
// 三个页面显示同一个名字，不各写一份。
const varietyNames = ref<Record<string, string>>({})
const router = useRouter()

async function loadVarietyNames() {
  try {
    const { data } = await getSpreadVarieties('self')
    varietyNames.value = Object.fromEntries(data.items.map((item) => [item.symbol, item.name]))
  } catch {
    // 取不到就退回代码。少个中文名，好过因为一次取名失败让整张表打不开。
  }
}

async function load() {
  loading.value = true
  try {
    const { data } = await getSpreadMonitor(
      threshold.value,
      tradeDate.value || undefined,
      historyMode.value
    )
    items.value = data.items
    asOf.value = data.as_of
    availableDates.value = data.available_dates
    if (!historyMode.value && !tradeDate.value && data.as_of) tradeDate.value = data.as_of
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '套利监控读取失败')
    items.value = []
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadVarietyNames()
  void load()
  void loadFundFlow()
  void loadBounce()
  void loadJdRoll()
})
watch([threshold, tradeDate, historyMode], () => void load())

const availableDateSet = computed(() => new Set(availableDates.value))
function isNotTradingDay(day: Date) {
  const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
  return !availableDateSet.value.has(key)
}

function label(instrument: string) {
  return varietyNames.value[instrument] ?? instrument
}
function comboName(item: SpreadMonitorItem) {
  return item.is_cross_variety
    ? `${label(item.instrument_1)} ${item.contract_1} − ${label(item.instrument_2)} ${item.contract_2}`
    : `${item.contract_1} − ${item.contract_2}`
}

const varieties = computed(() => {
  const seen = new Set<string>()
  for (const item of items.value) {
    seen.add(item.instrument_1)
    seen.add(item.instrument_2)
  }
  return [...seen].sort()
})

/** 离中线多远。越大越极端；排序靠它，越极端的排越前。 */
function extremity(item: SpreadMonitorItem) {
  const values = [item.pair, item.years]
    .filter((track): track is SpreadMonitorTrack => Boolean(track))
    .map((track) => (track.position === null ? null : Math.abs(Number(track.position) - 0.5)))
    .filter((value): value is number => value !== null && Number.isFinite(value))
  return values.length ? Math.max(...values) : -1
}

const filtered = computed(() => {
  const wanted = new Set(varietyFilter.value)
  return items.value
    .filter((item) => {
      if (wanted.size && !wanted.has(item.instrument_1) && !wanted.has(item.instrument_2)) {
        return false
      }
      if (direction.value !== 'all' && (item.alert ?? item.turn) !== direction.value) return false
      // 「仅新触发」只筛触发中那一组；已拐头与未触发的行没有新旧之分，
      // 让它们跟着一起消失会让人以为下面那半屏也被过滤了。
      if (onlyNew.value && item.alert && !item.is_new_alert) return false
      if (onlyEntry.value && !isEntry(item)) return false
      return true
    })
    // 干净的进场信号最上,信号差的进场其次,其余按离中线远近排(运营者要求)。
    .sort((a, b) => entryRank(b) - entryRank(a) || extremity(b) - extremity(a))
})

/** ⚡ 进场 = 今天刚拐头 × 本轮首次穿线 × 资格合格 × 未进交割红线。
 * 红线内不是没机会,是《体系》的硬纪律+数据实证的负期望区,不当进场信号。
 * 「首次」用 turn_crosses===1 判(DEC-070,运营者拍板):20 日窗内的第二次及
 * 以后穿线只挂 ⚠ 信号差,不再亮 ⚡;窗滑过之后的新穿线计数归 1,算新一轮。 */
function isQualifiedEntry(item: SpreadMonitorItem) {
  return (
    item.is_new_turn &&
    item.turn_crosses === 1 &&
    item.revert !== null &&
    isQualified(item.revert) &&
    !isRedLine(item.days_left)
  )
}
/**
 * ⚡ 反弹进场 · 生猪专用(DEC-121,运营者 2026-08-23 拍板):生猪跨月价差**低位**新拐头
 * (首次穿线、未进红线)直接亮,**不过「持到期 > 0」的合格门**。
 *
 * 为什么只给生猪、只给低位:运营者判断 2026 是磨底年,要做的是移仓换月带来的反弹
 * (择时平仓,不拿到期),合格门用「持到期」评资格天然把反弹型组合全部排除 ——
 * 2026 年生猪 14 次低位拐头 20 日均 +22.8%、胜 93%,被合格门拦掉的 10 次均 +24.4% 全胜
 * (REPORT_LH_TURN_ASSOC_v1)。拐头与机构/散户减仓关联弱(相关 +0.24 / +0.10),
 * 7/24 价差触底比机构卸仓(8/4 起)早 7 个交易日,所以不挂机构条件,直接按价差拐头。
 * **这是按年份判断开的门,不是全样本验证**:2025 同类事件好坏参半(−114% 到 +97%)。
 * 磨底年结束要回头关掉或重验 —— 写在 DEC-121 里。
 */
function isLhBounceEntry(item: SpreadMonitorItem) {
  return (
    !item.is_cross_variety &&
    item.instrument_1 === 'LH' &&
    item.turn === 'low' &&
    item.is_new_turn &&
    item.turn_crosses === 1 &&
    !isRedLine(item.days_left)
  )
}
function isEntry(item: SpreadMonitorItem) {
  return isQualifiedEntry(item) || isLhBounceEntry(item)
}
/** 排序权重:干净进场 2 > 信号差进场 1 > 其余 0。 */
function entryRank(item: SpreadMonitorItem) {
  if (!isEntry(item)) return 0
  return isChoppy(item.turn_crosses) ? 1 : 2
}

// 「触发中」与「已拐头」同列展示:拐头行多半已退出报警带,只按 alert 分组的话,
// 恰恰在该进场的时候它掉进「未触发」堆里,规则第二层就白做了。
// 历史模式下 fired = 历来的进场行(新日期在前),quiet 不展示。
const fired = computed(() =>
  historyMode.value
    ? filtered.value
        .filter((item) => isEntry(item))
        .sort((a, b) => b.trade_date.localeCompare(a.trade_date))
    : filtered.value.filter((item) => item.alert || item.turn)
)
const quiet = computed(() =>
  historyMode.value ? [] : filtered.value.filter((item) => !item.alert && !item.turn)
)
const historyDays = computed(() => new Set(fired.value.map((item) => item.trade_date)).size)

// 历史信号翻页(仿机构资金的历史信号列表):跨年全量有几百笔,一页 20 笔。
// 当前模式不翻页——当天的列表本来就该一眼看全。
const HISTORY_PAGE_SIZE = 20
const historyPage = ref(1)
watch([historyMode, varietyFilter, direction], () => {
  historyPage.value = 1
})
const pagedFired = computed(() => {
  if (!historyMode.value) return fired.value
  const start = (historyPage.value - 1) * HISTORY_PAGE_SIZE
  return fired.value.slice(start, start + HISTORY_PAGE_SIZE)
})
const highCount = computed(() => fired.value.filter((item) => item.alert === 'high').length)
const lowCount = computed(() => fired.value.filter((item) => item.alert === 'low').length)
const newCount = computed(() => fired.value.filter((item) => item.is_new_alert).length)
const turnCount = computed(() => fired.value.filter((item) => item.turn).length)
const qualifiedCount = computed(
  () => fired.value.filter((item) => item.revert && isQualified(item.revert)).length
)
const entryCount = computed(() => fired.value.filter((item) => isEntry(item)).length)

const CHOPPY_HINT =
  '近 20 个交易日内穿线 2 次及以上 —— 拐头反复。JM 09-01 实例:八天三次穿线,' +
  '期间价差打回区间顶,前两次进场按「创报警后新高离场」都得止损。' +
  '次数越多信号越弱,降档仓位或放过。'

const REDLINE_HINT =
  '距可交易窗口止点 ≤15 个交易日(《体系》红线:交割前 15 个交易日全部清仓)。' +
  '留一法数据:合格段剩余 <15 日持到底中位 −21.7%,为正仅 20%。' +
  '红线内 ⚡ 进场信号被压制;已有持仓按纪律清仓。'
const DECAY_HINT =
  '距窗口止点 16~39 个交易日 —— 利润衰减区。留一法数据:合格段 15~40 日桶持到底' +
  '中位 −32.5%,>40 日 +54.8%。可做,但降档仓位,盈利主要来自剩余 40 天以上的机会。'

const ENTRY_HINT =
  '今天刚拐头(位置今天才穿过回撤线)且资格合格 —— 回放口径里的进场日。' +
  '统计是盘后算的,执行等于次日进场。同一轮拐头只亮首次:20 日内第二次及以后的' +
  '穿线不再给进场标,只挂 ⚠ 信号差(DEC-070,运营者拍板)。'

const LH_BOUNCE_HINT =
  '生猪专用(DEC-121,运营者 2026-08-23 拍板):跨月价差低位刚拐头(今天穿过回撤线、本轮首次、' +
  '未进红线)直接亮,不看「持到期」合格门。依据:2026 年 14 次低位拐头 20 日均 +22.8%、' +
  '胜 93%,被合格门拦掉的 10 次均 +24.4% 全胜;机制是移仓换月。**出场用择时(移动止盈 1/3)' +
  ',不拿到期。** 这是按磨底年的判断开的门,不是全样本验证 —— 2025 同类事件好坏参半。'

const TURN_HINT =
  '近 20 个交易日内当年轨曾进 3% 报警带，当前已自极值回撤超过该品种的档位' +
  '（全量回测分档:焦煤 20%、鸡蛋 5%、玻纯跨品种 8%、其余 10%——回撤线画在' +
  '位置刻度上,焦煤位置日抖动全场最高要深线,鸡蛋季节趋势强早进不受罚要浅线）。' +
  '分层规则的进场信号：报警只是机会出现，拐头才是上车点——全样本回放里报警当天' +
  '就进持到底中位为负，等拐头才转正。'
/** 平台位阶梯里挑出止损与目标(DEC-095)。角色是后端按交易侧派好的,这里只取。 */
function stopShelf(item: SpreadMonitorItem) {
  return item.shelves?.find((s) => s.role === 'stop') ?? null
}
/** 目标按「离现价最近」排,第一个就是第一目标。 */
function targetShelves(item: SpreadMonitorItem) {
  return (item.shelves ?? [])
    .filter((s) => s.role === 'target')
    .sort((a, b) => Math.abs(Number(a.offset)) - Math.abs(Number(b.offset)))
}
/** 「历年点数」那一格的悬停:三个跨起点搬不动的数,连同为什么把它们收起来。 */
function legacyPointsText(item: SpreadMonitorItem) {
  const r = item.revert
  if (!r) return ''
  const n = (v: string | null) => (v === null ? '—' : Math.round(Number(v)).toString())
  return (
    `历年 ${r.hit}/${r.n} 年(${revertPct(r)})曾在窗口内朝这边动过(1 个点、1 天就算,` +
    '剩余期一长趋近 100%,**几乎没有区分度**,所以不再摆在行内)。 ' +
    `历年同一日历位置起算的中位数:最有利 ${points(r.move_points) ?? '—'} 点 · ` +
    `持到期 ${points(r.drift_points) ?? '—'} 点 · 跳空最坏参考 −${n(r.mae_max_points)} 点。 ` +
    '**这三个数跨起点搬不动**:历年各年的起点相差几千点,今天的价差常常落在它们的' +
    '分布之外,直接拿点数当目标、或当仓位分母,都不对。行内的平台位阶梯给的是' +
    '**这一对合约自己图上的位置**,那才是能用的。 ' +
    '`持到期` 的**符号**仍然在用——✓合格 徽标要求它为正,它防的是「回归率 100% ' +
    '却一路朝反方向走」那种陷阱。 ' +
    '「跳空最坏参考」是历年最坏那一年的浮亏极值。**它不再是仓位分母**:有了止损之后' +
    '仓位按到止损的距离算,它只回答「止损被跳空穿掉能穿多远」。'
  )
}

const DIRECTION_HINT =
  '同一个位置,两个方向的风险常常很不对称(DEC-098)。这里并排给出**各自的**' +
  '第一目标、止损与盈亏比 —— 盈亏比 = 到第一目标的距离 ÷ 到止损的距离,' +
  '**<1 就是赚的没有亏的多**。概率是那一档的到达概率(长期频率,逐年 17%~53%,' +
  '**不含方向判断**)。带 ⚡ 的那一侧是信号指的方向;另一侧只作对照,不代表建议做它。'

const SHELF_HINT =
  '平台位 = 价差自己走出来的横盘转折位(收盘是前后各 3 个交易日里的极值,' +
  '50 点内并成一档)。触碰回合 = 收盘落在该档 ±25 点内的独立回合数。' +
  '**最近三个交易日的转折还不算数**——它要等 3 天才确认,不然历史行会带上未来信息。'

const REACH_HINT =
  '到达概率 = 历史上从同样远近(距离 ÷ σ√剩余交易日)的处境出发,在窗口止点前' +
  '摸到过这个距离的比例,14.4 万个观测。**它不含方向判断**:上下两侧用同一条曲线,' +
  '方向由「日线收盘突破平台位」那条规矩定。**逐年离散很大**:同样的距离,' +
  '2014 年只有 17%、2019 年有 53%,长期中位 42%。逐品种/逐方向的版本样本外崩了' +
  '(那是行情漂移不是品种特性),所以合并掉了。'

const CONFLICT_HINT =
  '两条轨在讲相反的故事:一条说该做多价差,另一条说该做空价差。' +
  '上面的徽标与统计一律按**拐头侧**(要做的那笔)给,下面单列出另一侧供对照。' +
  'DEC-088 之前只显示其中一侧,合格标判的是 A 方向、进场标指的是 B 方向,' +
  '叠在一起会放行一笔没有统计支持的交易。两侧打架时降档或放过。'

const QUALIFIED_HINT =
  '判据只有一条:历年「持到期」为正 —— 从这个日历位置一路持到窗口止点,历年中位是' +
  '朝回归方向走的。留一法回放:合格的报警段持到底中位 +29% 区间,不合格的 −26%。' +
  '不合格的行没有徽标——没有徽标就是「别做」。' +
  '**原来还要求「历年触及率 ≥80%」,DEC-098 去掉了**:那条判的是「窗口内曾经朝这边' +
  '动过一次」,1 个点、1 天就算,剩余期一长趋近 100%。全表 84% 的行两侧都过它、' +
  '41% 的行两侧同时是 100%,它筛不掉任何东西,留着只会让人以为资格是两条件把关。'

/**
 * 玻纯「永安对冲簿」状态卡(DEC-142,展示级)。引擎在 pair_fgsa.json 里合成,
 * 这里只读不算(DEC-104:判据/统计在引擎,前端只渲染)。
 * 永安在两品种主力净持仓反向时跟它方向持价差,同向不在场。
 * **背景,不是进场信号**;丑话(知情上一档/利润前重后轻)在引擎 note 里,不抄第二份。
 *
 * 「资金流向」行(DEC-087)2026-08-25 运营者拍板**删除**(DEC-144):它当规则被
 * 测死过(REPORT_FGSA_LINK_v1 C2),与过闸的对冲簿并排权重感等同会稀释信号;
 * 引擎照算 pair_fgsa.json 的 z/note 留档,要挂回来只动这个文件。
 */
interface HedgeBook {
  member: string
  data_date: string
  state: 'opposite' | 'same' | null
  /** long=多玻空碱=做多价差;short=空玻多碱=做空价差(玻璃恒前腿,运营者口径)。 */
  direction: 'long' | 'short' | null
  fg_net: number | null
  sa_net: number | null
  fg_main: string
  sa_main: string
  seg_start: string | null
  seg_days: number | null
  seg_ret_pct: number | null
  stats: { cum_pct: number; sharpe: number | null; max_dd_pct: number; in_market_pct: number }
  note: string
}
const hedgeBook = ref<HedgeBook | null>(null)

/** 文案用**做多/做空价差**(运营者 2026-08-25 定的口径,玻璃恒前腿):
 *  多玻空碱=做多价差,空玻多碱=做空价差 —— 不用"做扩/做缩",那是价格位置的词,
 *  仓位方向的词是多空。定义锚在 FG 上,与行的腿序无关(建组 SQL 恒 a=FG)。 */
function hedgeBookFor() {
  const hb = hedgeBook.value!
  if (hb.state !== 'opposite' || !hb.direction) {
    return { on: false, long: false, text: `永安两腿同向 · 不在场(玻 ${fmtNet(hb.fg_net)}/碱 ${fmtNet(hb.sa_net)})` }
  }
  const long = hb.direction === 'long'
  const legs = long ? `多${hb.fg_main} 空${hb.sa_main}` : `空${hb.fg_main} 多${hb.sa_main}`
  return {
    on: true,
    long,
    text: `永安两边吃 · ${long ? '做多价差' : '做空价差'}(${legs})第 ${hb.seg_days ?? '—'} 日`
  }
}

function fmtNet(v: number | null): string {
  return v === null ? '—' : v.toLocaleString('zh-CN')
}

/** 两腿成本 —— **直接引净持仓页那台成本引擎的数**(运营者 2026-08-25:「成本直接
 *  引用净持仓的成本,不需要你单独算」):浏览器带登录态调 /seats/net-position,
 *  取 latest_members 里永安对应腿的 long_cost/short_cost,与净持仓页逐家那排
 *  同源同数。取不到(掉榜/接口错)就不显示,不自己推。 */
const hedgeCosts = ref<{ fg: string | null; sa: string | null }>({ fg: null, sa: null })

async function loadHedgeCosts() {
  const hb = hedgeBook.value
  if (!hb) return
  const one = async (instrument: 'FG' | 'SA', contract: string, net: number | null) => {
    if (net === null || net === 0) return null
    try {
      const res = await getSeatNetPosition({ instrument, members: [hb.member], contract })
      const leg = res.data.latest_members.find((m: MemberLeg) => m.member === hb.member)
      if (!leg || leg.missing) return null
      const cost = net > 0 ? leg.long_cost : leg.short_cost
      return cost === null ? null : Number(cost).toFixed(2)
    } catch {
      return null
    }
  }
  const [fg, sa] = await Promise.all([
    one('FG', hb.fg_main, hb.fg_net),
    one('SA', hb.sa_main, hb.sa_net)
  ])
  hedgeCosts.value = { fg, sa }
}

/** 只认玻璃×纯碱这一个跨品种组合——信号是为它算的,别挂到别的行上去。 */
function isFgSa(item: SpreadMonitorItem): boolean {
  if (!item.is_cross_variety) return false
  return [item.instrument_1, item.instrument_2].sort().join('-') === 'FG-SA'
}

// fundFlowFor(资金流向的走扩/收窄文案)随 DEC-144 一起删了。谁要恢复这行,记住
// 0 轴口径(运营者 2026-08-25):收窄=价差向 0 轴靠近,与当前价差在哪一侧有关,
// 不能拿 z 的符号直译成走扩——价差 −132 时"玻璃更强"是收窄。

/**
 * 生猪「卸仓反弹」窗口(DEC-119)。引擎(DEC-118)按日写在 hog_signals.json 的
 * `bounce_long` 里,这里只读不算;只挂在生猪的跨月组合上。
 * 与 FG-SA 资金流向同一性质:**背景,不是进场信号**。取不到就不显示。
 */
const bounce = ref<BounceState | null>(null)
/** 逐日历史(DEC-120):每一行按**它自己的交易日**取当天状态,不是最新状态。 */
const bounceHistory = ref<BounceDay[]>([])

function isLhCalendar(item: SpreadMonitorItem): boolean {
  return !item.is_cross_variety && item.instrument_1 === 'LH'
}

/** 这一行该显示的窗口状态。历史里找不到那天(早于起点)就不显示,不拿最新的顶。 */
function bounceAt(item: SpreadMonitorItem): BounceState | null {
  if (!bounce.value) return null
  if (!bounceHistory.value.length) return bounce.value
  const row = pickBounceDay(bounceHistory.value, item.trade_date)
  if (!row) return null
  return { ...bounce.value, active: row.active, unload: row.unload, side: row.side }
}
/** 当年位置(0~1),给反弹文案判高位/低位(DEC-128)。 */
function pairPos(item: SpreadMonitorItem): number | null {
  const p = item.pair.position
  return p === null ? null : Number(p)
}
/** 状态取自哪一天 —— 与行的交易日不同时(周末/节假日退到前一天)要写出来。 */
function bounceDate(item: SpreadMonitorItem): string | null {
  const row = bounceHistory.value.length ? pickBounceDay(bounceHistory.value, item.trade_date) : null
  return row && row.d !== item.trade_date ? row.d : null
}

async function loadBounce() {
  try {
    const res = await fetch(`/smart-money/hog_signals.json?t=${Date.now()}`)
    if (!res.ok) return
    const data = (await res.json()) as { bounce_long?: BounceState | null; bounce_history?: BounceDay[] | null
      roll_pressure?: RollPressureState | null }
    bounce.value = data.bounce_long ?? null
    bounceHistory.value = data.bounce_history ?? []
    rollPressures.value.LH = data.roll_pressure ?? null
  } catch {
    // 背景信息取不到就不显示,页面主体照常
  }
}

/** 移仓压力:挂在含近月主力那条腿的跨月组合上,窗口内才显示。
 *  生猪(DEC-136/137,⚡做空价差)+ 鸡蛋(DEC-145,判据级双向含镜像⚡做多价差);
 *  焦煤展示级只在品种页,不进套利监控(REPORT_JM_THREE_GAPS_v1:判据无区分度,
 *  这里的行内位是给"可操作"的信号留的)。 */
const rollPressures = ref<Record<string, RollPressureState | null>>({ LH: null, JD: null })
function rollAt(item: SpreadMonitorItem): RollPressureState | null {
  if (item.is_cross_variety) return null
  const rp = rollPressures.value[item.instrument_1] ?? null
  if (!rp || !rp.active) return null
  return item.contract_1 === rp.main || item.contract_2 === rp.main ? rp : null
}

async function loadJdRoll() {
  try {
    const res = await fetch(`/smart-money/jd_signals.json?t=${Date.now()}`)
    if (!res.ok) return
    const data = (await res.json()) as { roll_pressure?: RollPressureState | null }
    rollPressures.value.JD = data.roll_pressure ?? null
  } catch {
    // 背景信息取不到就不显示,页面主体照常
  }
}

async function loadFundFlow() {
  try {
    const res = await fetch(`/smart-money/pair_fgsa.json?t=${Date.now()}`)
    if (res.ok) {
      const data = (await res.json()) as { hedge_book?: HedgeBook | null }
      // 对冲簿卡与 pair 信号同一个 JSON(DEC-142);老 JSON 没有这个键 -> 不显示。
      // z/direction 那部分(资金流向行)DEC-144 已从页面删除,引擎照写留档。
      hedgeBook.value = data.hedge_book ?? null
      // 腿成本另取自净持仓成本引擎(见 loadHedgeCosts 注释),失败只影响 @成本 一截。
      void loadHedgeCosts()
    }
  } catch {
    // 背景信息取不到就不显示,页面主体照常
  }
}

const BASIS_HINT =
  '现货价与主力基差(生意社数据,DEC-074)。基差 = 现货 − 主力期货:为正是期货' +
  '贴水(现货更贵),为负是期货升水。**跨期套利的两条腿相对同一个现货,所以两个' +
  '基差之差就是价差本身**——这里给的是水平与历史分位,是产业背景不是进场信号。' +
  '苹果没有现货报价,那几行不显示。跨品种组合按第一条腿的品种给。'

const REVERT_HINT =
  '同月份组合（同品种 + 同月份对 + 同年差）跨年拼起来的样本，不是这一组合自己的胜率。' +
  '可交易窗口照 5A 那套：止点＝先到期那条腿的散户最后交易日。历年按月-日对齐，' +
  '从今天这个日历位置一直看到各自窗口的止点，期间任何一天触及即算回归（不比终点）。' +
  '只用已走完的年份。剩余期越长回归率越接近 100%，所以要连着「持到期」一起看：' +
  '它为负说明历年这段最终是朝反方向走的。'

/** 圆点在轨道上的位置。位置可以落在 0~1 之外（历年轨用的是百分位区间），画到边上为止。 */
function markLeft(track: SpreadMonitorTrack) {
  if (track.position === null) return null
  const value = Number(track.position)
  if (!Number.isFinite(value)) return null
  return `${Math.min(100, Math.max(0, value * 100))}%`
}
function pct(track: SpreadMonitorTrack) {
  if (track.position === null) return '—'
  const value = Number(track.position)
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'
}
function num(text: string) {
  const value = Number(text)
  return Number.isFinite(value) ? value.toLocaleString('zh-CN') : text
}
function signedSpread(text: string) {
  const value = Number(text)
  if (!Number.isFinite(value)) return text
  return value > 0 ? `+${value.toLocaleString('zh-CN')}` : value.toLocaleString('zh-CN')
}

// —— 手工产业备注(DEC-069):统计说「历年数字怎么说」,备注说「为什么」——
// 运营者从直播/盖楼学来的品种级知识(夏天的煤不能空、纯碱低库存是逼空窗口…)。
// 备注挂在**月份模板**上:JD2609−JD2701 和 JD2709−JD2801 共享同一条「09-01」。
const auth = useAuthStore()
const noteDialogOpen = ref(false)
const noteDraft = ref('')
const noteSaving = ref(false)
const noteTarget = ref<SpreadMonitorItem | null>(null)

function templateName(item: SpreadMonitorItem) {
  const m1 = item.contract_1.slice(-2)
  const m2 = item.contract_2.slice(-2)
  return item.is_cross_variety
    ? `${label(item.instrument_1)}${m1} − ${label(item.instrument_2)}${m2}`
    : `${label(item.instrument_1)} ${m1}-${m2}`
}

function openNoteEditor(item: SpreadMonitorItem) {
  noteTarget.value = item
  noteDraft.value = item.note ?? ''
  noteDialogOpen.value = true
}

async function saveNote() {
  const item = noteTarget.value
  if (!item) return
  noteSaving.value = true
  try {
    if (!auth.csrfToken) await auth.loadCsrf()
    if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
    const note = noteDraft.value.trim()
    await saveSpreadTemplateNote(
      {
        instrument_1: item.instrument_1,
        month_1: Number(item.contract_1.slice(-2)),
        instrument_2: item.instrument_2,
        month_2: Number(item.contract_2.slice(-2)),
        note
      },
      auth.csrfToken
    )
    // 同一模板可能对应表里好几行(JD 09-01 有多个年份对),本地全刷一遍,
    // 不用整页重查。
    const key = (x: SpreadMonitorItem) =>
      `${x.instrument_1}|${x.contract_1.slice(-2)}|${x.instrument_2}|${x.contract_2.slice(-2)}`
    const target = key(item)
    for (const row of items.value) {
      if (key(row) === target) row.note = note || null
    }
    noteDialogOpen.value = false
    ElMessage.success(note ? '备注已保存' : '备注已清空')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '备注保存失败')
  } finally {
    noteSaving.value = false
  }
}

// 带着这一组合约跳到自由价差页，那边会自动填好并查出来（见 applyDeepLink）。
// 传品种**代码**不传中文名：中文名在 product_instrument_scope 里可改，
// 拿会变的东西做链接参数迟早对不上。
function openDetail(item: SpreadMonitorItem) {
  void router.push({
    path: '/spread-analytics/free-spread',
    query: {
      symbol1: item.instrument_1,
      month1: item.contract_1.slice(-2),
      symbol2: item.instrument_2,
      month2: item.contract_2.slice(-2)
    }
  })
}
</script>

<template>
  <section class="page monitor">
    <header class="page-heading">
      <h1>套利监控</h1>
      <p>
        盯住每一组合约的价差，看它是不是走到了历史极值。触发与否是按下面的阈值现算的，
        换个阈值同一天的结论就跟着变。
      </p>
      <p class="caveat">
        <strong>贴到极值不等于会回归。</strong>
        每组后面是它自己的历年成绩：从今天这个日历位置起，到该组合退出散户可交易区间为止，
        历年有几年曾经回落、最有利时能走多少点、一路持到最后又是多少。
        剩余时间越长「曾经回归」越容易达成，所以别只看那个百分比——
        <strong>持到期为负</strong>就说明历年这段最终是朝反方向走的。
        三步用法：只看带 <strong>✓ 合格</strong> 的行；<strong>⚡ 进场</strong> 亮的当晚
        就是信号日（次日执行），带它的行排在最上面，<strong>方向就写在标上</strong>——
        「做空价差」= 卖腿1买腿2，「做多价差」= 买腿1卖腿2。
        <!-- 拐头分档写死在这段文案里(运营者 2026-08-24 要求写明):值必须与
             deploy/collector/compute-spread-monitor.sql 的分档表和 monitor.rs 的
             turn_retreat() 保持一致(DEC-070 定档、DEC-075 AP 退回默认),改那边要同步这里。 -->
        <strong>「已拐头」怎么判</strong>：近 20 个交易日内当年轨曾进 3% 报警带，且已自极值
        <strong>回撤超过区间宽度的一个比例</strong>——这个比例按品种分档（DEC-070 全量回测定的）：
        焦煤 20%、鸡蛋 5%、玻璃-纯碱跨品种 8%、其余（含生猪跨月）10%。⚡ 只在<strong>本轮首次</strong>穿线那天亮。
        <strong>唯一例外是生猪跨月的橙色「⚡ 反弹进场」</strong>（DEC-121）：低位刚拐头就亮、
        <strong>不过合格门</strong>，做多价差、移动止盈 1/3 出场、不拿到期——按 2026 磨底年的判断开的门，不是全样本验证。
        生猪行上的「反弹窗口」（已卸掉多少 · 反弹参考区间 10~26% · 价差高/低位）是<strong>背景参考</strong>，
        不是进场信号（DEC-128）。<strong>仓位按到止损的距离算</strong>:
        可承受亏损 ÷ (到止损点数 × 点值) = 手数 —— 止损就摆在平台位那一行上。
        浮亏到「历年常态浮亏」是常态,不是逻辑坏了。
        (旧口径按「风险预留」那个历年最坏 MAE 算,与止损差出一两个数量级,已停用;
        那个数收进「历年点数 ⓘ」,只当止损被跳空穿掉的最坏参考。)
        <strong>剩余 ≤15 交易日进交割红线</strong>,⚡ 压制、持仓清掉;16~39 日是衰减区,降档。
        「已拐头」还挂着但 ⚡ 已灭的，是进场日已过的存量状态；
        带 <strong>⚠ 信号差</strong> 的是 20 日内反复拐头的组合——降档仓位或放过。
        没有徽标的报警，当风景。
      </p>
    </header>

    <el-card shadow="never" class="controls">
      <div class="control-row">
        <!-- popper-class 与日历同一个理由(2026-08-31 运营者报「点了没反应」):
             el-select 的下拉同样传送到 body、同样带进场过渡,连点时卡在
             opacity≈0 —— 面板已展开(输入框聚焦、箭头朝上)但看不见。
             当初只给日历关了过渡,**这两个下拉漏了**,是同一个 bug 的另一半。 -->
        <el-select
          v-model="varietyFilter"
          multiple
          collapse-tags
          collapse-tags-tooltip
          clearable
          placeholder="全部品种"
          popper-class="spread-select-popper"
          style="width: 220px"
        >
          <el-option v-for="code in varieties" :key="code" :label="label(code)" :value="code" />
        </el-select>

        <el-select v-model="threshold" popper-class="spread-select-popper" style="width: 190px">
          <el-option v-for="opt in THRESHOLDS" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>

        <!-- 点击即弹面板:原来输入框可编辑,点进去是在文字里放光标,面板时开时不开
             (2026-08-23 运营者报「有时候不弹」);也不再因为可选日期列表暂时为空就整体禁用——
             列表没回来之前面板里全部日子不可选,但面板本身要能打开。历史信号模式下仍禁用
             (那个模式不按单日看)。 -->
        <!-- 日历「弹不出来」(运营者 2026-08-23 两次报;Chrome 实测复现):双击/连点输入框时
             popper 的 el-zoom-in-top 进场过渡被打断,卡在 enter-active、opacity≈0 —— 面板已经
             display:block 但看不见,要好几秒才慢慢显出来。关掉这个 popper 的过渡动画就立刻显示
             (页内注入同样 CSS 实测有效)。popper 传送到 body,样式写在下面的非 scoped 块里。 -->
        <el-date-picker
          v-model="tradeDate"
          type="date"
          style="width: 170px"
          placeholder="交易日"
          value-format="YYYY-MM-DD"
          popper-class="spread-date-popper"
          :clearable="false"
          :editable="false"
          :disabled-date="isNotTradingDay"
          :disabled="historyMode"
        />

        <el-radio-group v-model="direction">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="high">高位</el-radio-button>
          <el-radio-button value="low">低位</el-radio-button>
        </el-radio-group>

        <el-checkbox v-model="onlyNew" border :disabled="historyMode">仅新触发</el-checkbox>
        <el-checkbox v-model="onlyEntry" border :disabled="historyMode">仅进场日</el-checkbox>
        <el-tooltip
          content="一次列出历年全部 ⚡ 进场信号(同月份模板的历年组合都在内,过期组合带灰标),新日期在前,可翻页。"
          placement="top"
        >
          <el-checkbox v-model="historyMode" border>历史信号</el-checkbox>
        </el-tooltip>
      </div>

      <div class="tally" v-if="!historyMode">
        <div class="cell"><span class="k">监控组合</span><span class="v">{{ items.length }}</span></div>
        <div class="cell"><span class="k">触发</span><span class="v">{{ fired.length }}</span></div>
        <div class="cell"><span class="k">新触发</span><span class="v fresh">{{ newCount }}</span></div>
        <div class="cell"><span class="k">已拐头</span><span class="v fresh">{{ turnCount }}</span></div>
        <div class="cell"><span class="k">✓ 合格</span><span class="v qual">{{ qualifiedCount }}</span></div>
        <div class="cell"><span class="k">⚡ 进场</span><span class="v entry">{{ entryCount }}</span></div>
        <div class="cell"><span class="k">高位</span><span class="v high">{{ highCount }}</span></div>
        <div class="cell"><span class="k">低位</span><span class="v low">{{ lowCount }}</span></div>
        <div class="cell" v-if="asOf"><span class="k">数据日</span><span class="v date">{{ asOf }}</span></div>
      </div>
      <div class="tally" v-else>
        <div class="cell"><span class="k">⚡ 历史进场信号</span><span class="v entry">{{ fired.length }}</span></div>
        <div class="cell"><span class="k">覆盖信号日</span><span class="v">{{ historyDays }}</span></div>
        <div class="cell" v-if="asOf"><span class="k">最新快照</span><span class="v date">{{ asOf }}</span></div>
      </div>
    </el-card>

    <el-empty v-if="!loading && !items.length" description="这一天还没有监控快照" />

    <template v-else>
      <h2 class="group-label">{{ historyMode ? '历史进场信号 · 新日期在前' : '触发中 · 已拐头' }}</h2>
      <el-empty
        v-if="!fired.length"
        :description="historyMode ? '已存快照日里没有出现过 ⚡ 进场信号' : '按当前阈值没有组合触发,也没有已拐头的组合'"
        :image-size="70"
      />
      <div class="rows" v-loading="loading">
        <article
          v-for="item in pagedFired"
          :key="item.trade_date + item.contract_1 + item.contract_2"
          class="row"
          :class="(item.alert ?? item.turn) === 'high' ? 'fired-high' : 'fired-low'"
        >
          <div class="ident">
            <div class="pair">{{ comboName(item) }}</div>
            <div class="meta">
              <el-tag size="small" :type="item.is_cross_variety ? 'warning' : 'info'" effect="plain">
                {{ item.is_cross_variety ? '跨品种' : label(item.instrument_1) }}
              </el-tag>
              <el-tooltip
                v-if="item.is_new_alert"
                content="前一交易日按同一阈值还没触发，今天才进极值区。"
                placement="top"
              >
                <span class="badge-new">新</span>
              </el-tooltip>
              <span class="asof" v-if="historyMode || item.trade_date !== asOf">{{ item.trade_date }}</span>
            </div>
            <div class="now">{{ signedSpread(item.spread) }}</div>
          </div>

          <div class="tracks">
            <div class="track" v-for="t in [{ name: '当年', d: item.pair }, { name: '历年', d: item.years }]" :key="t.name">
              <span class="name">{{ t.name }}</span>
              <div v-if="t.d">
                <div class="rail">
                  <span class="zone lo" :style="{ width: `${threshold * 100}%` }"></span>
                  <span class="zone hi" :style="{ width: `${threshold * 100}%` }"></span>
                  <span
                    v-if="markLeft(t.d)"
                    class="mark"
                    :class="t.d.alert ?? ''"
                    :style="{ left: markLeft(t.d)! }"
                  ></span>
                </div>
                <div class="ends"><span>{{ num(t.d.low) }}</span><span>{{ num(t.d.high) }}</span></div>
              </div>
              <div v-else class="absent">历史不足</div>
              <span class="pct" :class="t.d?.alert ?? ''">{{ t.d ? pct(t.d) : '—' }}</span>
            </div>
          </div>

          <div class="tail">
            <div class="state-tags">
              <el-tag v-if="item.expired" type="info" effect="plain" size="small">
                已过期
              </el-tag>
              <el-tooltip v-if="isRedLine(item.days_left)" :content="REDLINE_HINT" placement="top">
                <span class="badge-redline">临近交割 · 剩 {{ item.days_left }} 日</span>
              </el-tooltip>
              <el-tooltip
                v-else-if="isDecayZone(item.days_left)"
                :content="DECAY_HINT"
                placement="top"
              >
                <span class="badge-decay">衰减区 · 剩 {{ item.days_left }} 日</span>
              </el-tooltip>
              <el-tooltip v-if="isQualifiedEntry(item) && item.revert" :content="ENTRY_HINT" placement="top">
                <!-- 方向必须写在标上(DEC-088):这个标此前只说「进场」,不说做多还是
                     做空,而它旁边的「✓ 合格」当时判的可能是反方向。 -->
                <span class="badge-entry">⚡ 进场 · {{ tradeDirection(item.revert) }}价差</span>
              </el-tooltip>
              <!-- 生猪反弹进场(DEC-121):低位拐头即亮,不过合格门;徽标与正规 ⚡ 区分开,
                   字面写明「反弹·择时平仓」,别让人当成拿到期的那种。 -->
              <el-tooltip v-else-if="isLhBounceEntry(item)" :content="LH_BOUNCE_HINT" placement="top">
                <span class="badge-entry bounce">⚡ 反弹进场 · 做多价差 · 择时平仓</span>
              </el-tooltip>
              <el-tag
                v-if="item.alert"
                :type="item.alert === 'high' ? 'danger' : 'success'"
                effect="light"
                size="small"
              >
                {{ item.alert === 'high' ? '高位' : '低位' }}
              </el-tag>
              <el-tooltip v-if="item.turn" :content="TURN_HINT" placement="top">
                <el-tag type="warning" effect="light" size="small">
                  <!-- 方向永远带上:LH2611−LH2705 出现过「高位报警 + 低位拐头」并存,
                       省掉后缀会让人把低位拐头读成高位的。 -->
                  已拐头{{ item.turn === 'high' ? ' · 高位' : ' · 低位' }}
                </el-tag>
              </el-tooltip>
              <el-tooltip v-if="isChoppy(item.turn_crosses)" :content="CHOPPY_HINT" placement="top">
                <span class="badge-choppy">⚠ 信号差 ×{{ item.turn_crosses }}</span>
              </el-tooltip>
              <el-tooltip v-if="item.revert_alt" :content="CONFLICT_HINT" placement="top">
                <span class="badge-choppy">⚠ 两侧方向相反</span>
              </el-tooltip>
              <el-tooltip
                v-if="item.revert && isQualified(item.revert)"
                :content="QUALIFIED_HINT"
                placement="top"
              >
                <span class="badge-q">✓ 合格 · {{ tradeDirection(item.revert) }}价差</span>
              </el-tooltip>
            </div>

            <el-tooltip v-if="item.revert" :content="REVERT_HINT" placement="top">
              <!-- 不再挂 revertTone:它原来只给那个大号数字上色,数字撤了它就是空类。 -->
              <div class="revert">
                <!-- 「曾回归 100%」那个大号数字撤了(DEC-098):它 63% 的时间就是
                     100%,却是全页面字号最大的数。收进悬停当背景,别当结论。 -->
                <span class="basis" v-if="item.revert.days">剩 {{ item.revert.days }} 天</span>
                <!-- 「最有利/持到期/风险预留」三个点数收进悬停(DEC-097)。
                     它们是历年从同一日历位置起算的中位数,**起点差几千点也照样相减**,
                     搬不到今天的处境上;有了平台位阶梯之后行内不再摆它们。
                     `持到期` 的**符号**仍然在用——✓合格 徽标就是靠它判的。 -->
                <el-tooltip :content="legacyPointsText(item)" placement="top">
                  <span class="basis legacy">历年点数 ⓘ</span>
                </el-tooltip>
                <span class="basis mae" v-if="points(item.revert.mae_points)">
                  历年常态浮亏 −{{ Math.abs(Number(item.revert.mae_points)).toFixed(0) }} 点
                </span>
              </div>
            </el-tooltip>
            <div v-else class="revert absent">历年无可比样本</div>

            <!-- 另一侧(DEC-088)。只在两侧方向相反时出现——藏起来正是那个 BUG 的做法。 -->
            <el-tooltip v-if="item.revert_alt" :content="CONFLICT_HINT" placement="top">
              <div class="revert alt">
                <span class="basis">
                  另一侧 · {{ tradeDirection(item.revert_alt) }}价差
                  {{ item.revert_alt.hit }}/{{ item.revert_alt.n }} 年曾回归
                  <template v-if="points(item.revert_alt.drift_points)">
                    · 持到期
                    <em :class="driftTone(item.revert_alt.drift_points)">
                      {{ points(item.revert_alt.drift_points) }}
                    </em>
                    点
                  </template>
                </span>
              </div>
            </el-tooltip>

            <el-tooltip v-if="item.basis" :content="BASIS_HINT" placement="top">
              <div class="basis">
                <span class="k">{{ label(item.basis.instrument) }}现货</span>
                <span class="v">{{ num(item.basis.spot_price) }}</span>
                <template v-if="item.basis.dominant_basis !== null">
                  <span class="k">主力基差</span>
                  <span class="v" :class="Number(item.basis.dominant_basis) >= 0 ? 'disc' : 'prem'">
                    {{ signedSpread(item.basis.dominant_basis) }}
                  </span>
                </template>
                <span class="pctile" v-if="item.basis.percentile !== null">
                  历年 {{ (Number(item.basis.percentile) * 100).toFixed(0) }}% 位
                </span>
              </div>
            </el-tooltip>

            <!-- 「资金流向」行(DEC-087)在这里挂过,DEC-144 删除——见 HedgeBook 注释。 -->
            <!-- 玻纯「永安对冲簿」状态卡(DEC-142,展示级)。只挂 FG-SA 这一个组合;
                 在场=永安两品种主力净持仓反向(历史约 31% 天数),方向跟它;
                 丑话在引擎 note(tooltip)里。**背景,不是进场信号。** -->
            <el-tooltip v-if="hedgeBook && isFgSa(item)" :content="hedgeBook.note" placement="top">
              <div class="basis fund">
                <span class="k">永安对冲簿</span>
                <span class="v" :class="hedgeBookFor().on ? (hedgeBookFor().long ? 'disc' : 'prem') : ''">
                  {{ hedgeBookFor().text }}
                </span>
                <span class="pctile" v-if="hedgeBookFor().on">
                  玻 {{ fmtNet(hedgeBook.fg_net) }}{{ hedgeCosts.fg ? ` @${hedgeCosts.fg}` : '' }}
                  / 碱 {{ fmtNet(hedgeBook.sa_net) }}{{ hedgeCosts.sa ? ` @${hedgeCosts.sa}` : '' }}
                </span>
                <span class="pctile">
                  回放 {{ hedgeBook.stats.cum_pct >= 0 ? '+' : '' }}{{ hedgeBook.stats.cum_pct }}% · 在场 {{ hedgeBook.stats.in_market_pct }}%
                </span>
                <span class="pctile flow-tip">展示级,不进判据</span>
              </div>
            </el-tooltip>

            <!-- 生猪跨月组合的「卸仓反弹」窗口(DEC-119)。引擎算,这里只读。
                 文案按 DEC-128(2026 回测):机构净空 + 本轮卸掉多少 + 反弹参考区间(历次价差触底时的卸仓比例
                 四分位 10~26%)+ 价差高位/低位,逻辑与数字都在 bounce-hint.ts 的 BOUNCE_REF/注释里,别在这里写死。
                 **背景,不是进场信号。** -->
            <el-tooltip v-if="isLhCalendar(item) && bounceAt(item)" :content="bounce!.note" placement="top">
              <div class="basis fund">
                <span class="k">反弹窗口</span>
                <span class="v" :class="bounceHint(bounceAt(item)!, pairPos(item)).on ? 'disc' : 'prem'">
                  {{ bounceHint(bounceAt(item)!, pairPos(item)).text }}
                </span>
                <span v-if="bounceDate(item)" class="pctile">按 {{ bounceDate(item) }} 机构状态</span>
                <span class="pctile flow-tip">背景参考,非进场信号</span>
              </div>
            </el-tooltip>
            <el-tooltip v-if="rollAt(item)" :content="rollAt(item)!.note" placement="top">
              <div class="basis fund">
                <span class="k">移仓压力</span>
                <span class="v" :class="rollPressureHint(rollAt(item)!).on ? 'disc' : 'prem'">
                  {{ rollPressureHint(rollAt(item)!).text }}
                </span>
                <!-- DEC-137 后这里是判据不再是纯背景,小字跟状态走(2026-08-25 运营者
                     指出旧话「非进场信号」与 ⚡ 自相矛盾,DEC-104 同款病)。 -->
                <span class="pctile flow-tip">
                  {{ rollAt(item)!.entry_flag ? '⚡ 已升判据 · 每届一次,持到交割纪律日' : '散户强制流,背景参考' }}
                </span>
              </div>
            </el-tooltip>

            <!-- 平台位(DEC-095)。摘要常驻一行,完整阶梯折起来——每行挂 8 档会把
                 整页压垮。用 <details> 是零 JS,与 admin 那批列设置同一个做法。 -->
            <details v-if="item.shelves && item.shelves.length" class="shelf">
              <summary>
                <span class="k">平台位</span>
                <template v-if="stopShelf(item)">
                  <!-- 距离要摆出来:它现在是**仓位分母**(DEC-097),不是装饰。 -->
                  <span class="chip stop">
                    止损 {{ shelfLabel(stopShelf(item)!) }}
                    <em>{{ Math.abs(Number(stopShelf(item)!.offset)) }} 点</em>
                  </span>
                </template>
                <template v-for="(t, i) in targetShelves(item).slice(0, 2)" :key="t.level">
                  <span class="chip target">
                    {{ i === 0 ? '第一目标' : '再看' }} {{ shelfLabel(t) }}
                    <em v-if="t.reach_pct !== null">{{ t.reach_pct.toFixed(0) }}%</em>
                  </span>
                </template>
                <span class="more">展开 {{ item.shelves.length }} 档</span>
              </summary>

              <div class="ladder">
                <div
                  v-for="s in item.shelves"
                  :key="s.level"
                  class="rung"
                  :class="{ above: Number(s.offset) > 0 }"
                >
                  <span class="lvl">{{ shelfLabel(s) }}</span>
                  <span class="meta">
                    触碰 <b>{{ s.touches }}</b> 回合 · {{ offsetText(s) }}
                    <em v-if="s.role === 'stop'" class="chip stop">止损</em>
                    <em v-else-if="s.role === 'target'" class="chip target">卖点</em>
                  </span>
                  <span class="pct">
                    <template v-if="s.reach_pct !== null">{{ s.reach_pct.toFixed(0) }}%</template>
                    <template v-else>—</template>
                  </span>
                </div>
                <p v-if="reachUnavailable(item.days_left)" class="reach-na">
                  剩 {{ item.days_left }} 个交易日,<b>不给到达概率</b>:这条曲线的样本
                  是剩余 5 日以上的处境,再往下没有依据。档位与距离照常。
                </p>
                <el-tooltip :content="DIRECTION_HINT" placement="top">
                  <table class="dir">
                    <thead>
                      <tr><th>方向</th><th>第一目标</th><th>止损</th><th>盈亏比</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="d in [true, false]" :key="String(d)"
                          :class="{ signal: (item.revert?.side === 'high') === d }">
                        <td>
                          {{ d ? '做空价差' : '做多价差' }}
                          <em v-if="(item.revert?.side === 'high') === d && isEntry(item)">⚡</em>
                        </td>
                        <template v-if="edgeOf(item, d)">
                          <td>{{ shelfLabel(edgeOf(item, d)!.target) }}
                            <i>{{ edgeOf(item, d)!.gain }} 点</i>
                            <b v-if="edgeOf(item, d)!.target.reach_pct !== null">
                              {{ edgeOf(item, d)!.target.reach_pct!.toFixed(0) }}%</b>
                          </td>
                          <td>{{ shelfLabel(edgeOf(item, d)!.stop) }}
                            <i>{{ edgeOf(item, d)!.risk }} 点</i></td>
                          <td :class="{ thin: (edgeOf(item, d)!.ratio ?? 0) < 1 }">
                            {{ edgeOf(item, d)!.ratio?.toFixed(2) ?? '—' }}
                          </td>
                        </template>
                        <td v-else colspan="3" class="none">这一侧没有可用的档位</td>
                      </tr>
                    </tbody>
                  </table>
                </el-tooltip>

                <p class="shelf-rule">
                  <b>日线收盘突破一档,才往下一档看</b>;反方向收盘站上最近那一档就止损。
                  <el-tooltip :content="SHELF_HINT" placement="top"><span class="q">档位怎么来的</span></el-tooltip>
                  <el-tooltip :content="REACH_HINT" placement="top"><span class="q">概率怎么来的</span></el-tooltip>
                  概率是<b>长期频率</b>(同样距离逐年 17%~53%),且<b>不含方向判断</b>。
                </p>
              </div>
            </details>

            <div v-if="item.note" class="note" @click="openNoteEditor(item)">
              📝 {{ item.note }}
            </div>

            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
              <el-button v-if="!item.note" link size="small" @click="openNoteEditor(item)">备注</el-button>
            </div>
          </div>
        </article>
      </div>

      <el-pagination
        v-if="historyMode && fired.length > HISTORY_PAGE_SIZE"
        v-model:current-page="historyPage"
        :page-size="HISTORY_PAGE_SIZE"
        :total="fired.length"
        layout="prev, pager, next, total"
        class="history-pager"
      />

      <h2 class="group-label" v-if="quiet.length">
        未触发
        <el-button link type="primary" size="small" @click="showQuiet = !showQuiet">
          {{ showQuiet ? '收起' : `展开 ${quiet.length} 组` }}
        </el-button>
      </h2>
      <div class="rows" v-if="showQuiet">
        <article v-for="item in quiet" :key="item.contract_1 + item.contract_2" class="row quiet">
          <div class="ident">
            <div class="pair">{{ comboName(item) }}</div>
            <div class="meta">
              <el-tag size="small" :type="item.is_cross_variety ? 'warning' : 'info'" effect="plain">
                {{ item.is_cross_variety ? '跨品种' : label(item.instrument_1) }}
              </el-tag>
            </div>
            <div class="now">{{ signedSpread(item.spread) }}</div>
          </div>
          <div class="tracks">
            <div class="track" v-for="t in [{ name: '当年', d: item.pair }, { name: '历年', d: item.years }]" :key="t.name">
              <span class="name">{{ t.name }}</span>
              <div v-if="t.d">
                <div class="rail">
                  <span class="zone lo" :style="{ width: `${threshold * 100}%` }"></span>
                  <span class="zone hi" :style="{ width: `${threshold * 100}%` }"></span>
                  <span v-if="markLeft(t.d)" class="mark" :style="{ left: markLeft(t.d)! }"></span>
                </div>
                <div class="ends"><span>{{ num(t.d.low) }}</span><span>{{ num(t.d.high) }}</span></div>
              </div>
              <div v-else class="absent">历史不足</div>
              <span class="pct">{{ t.d ? pct(t.d) : '—' }}</span>
            </div>
          </div>
          <div class="tail">
            <div v-if="item.note" class="note" @click="openNoteEditor(item)">
              📝 {{ item.note }}
            </div>
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
              <el-button v-if="!item.note" link size="small" @click="openNoteEditor(item)">备注</el-button>
            </div>
          </div>
        </article>
      </div>
    </template>

    <el-dialog
      v-model="noteDialogOpen"
      :title="noteTarget ? `产业备注 · ${templateName(noteTarget)}` : '产业备注'"
      width="480px"
    >
      <p class="note-hint">
        写给这个<strong>月份模板</strong>的品种知识,所有年份的同月组合共用一条
        (比如 JD 09-01 的备注,明年的 JD2709−JD2801 也会看到)。
        清空后保存即删除。
      </p>
      <el-input
        v-model="noteDraft"
        type="textarea"
        :rows="5"
        maxlength="2000"
        show-word-limit
        placeholder="例:夏天的煤不能做空(安监停产);12 月焦煤交割量大于 1 月"
      />
      <template #footer>
        <el-button @click="noteDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="noteSaving" @click="saveNote">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style>
/* 交易日日历与两个下拉的 popper(传送到 body,scoped 管不到):关掉过渡,
   避免连点时卡在 opacity 0。日历 2026-08-23 先修,下拉 2026-08-31 补上——
   同一个根因分两次才修全,教训是「同类组件要一次扫干净」。 */
.spread-date-popper.el-popper,
.spread-select-popper.el-popper {
  transition: none !important;
  opacity: 1 !important;
  transform: none !important;
}
</style>

<style scoped>
.controls {
  margin-bottom: 20px;
}
.control-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.tally {
  display: flex;
  flex-wrap: wrap;
  gap: 28px;
  margin-top: 16px;
}
.tally .cell {
  display: flex;
  flex-direction: column;
}
.tally .k {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--tv-text-muted);
  font-weight: 650;
}
.tally .v {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.tally .v.high {
  color: var(--tv-up);
}
.tally .v.low {
  color: var(--tv-down);
}
.tally .v.fresh {
  color: var(--tv-warn);
}
.tally .v.date {
  font-size: 16px;
  font-weight: 600;
  padding-top: 5px;
}

.group-label {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.06em;
  color: var(--tv-text-muted);
  margin: 22px 0 10px;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.row {
  display: grid;
  grid-template-columns: minmax(190px, 1fr) minmax(300px, 2.1fr) auto;
  gap: 22px;
  align-items: center;
  padding: 15px 17px;
  background: var(--tv-bg-card);
  border: 1px solid var(--tv-border);
  border-radius: 8px;
}
.row.fired-high {
  border-left: 3px solid var(--tv-up);
}
.row.fired-low {
  border-left: 3px solid var(--tv-down);
}
.row.quiet {
  background: var(--tv-bg-inset);
}

.ident {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.ident .pair {
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.ident .meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ident .asof {
  font-size: 11px;
  color: var(--tv-text-muted);
}
.ident .now {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

.tracks {
  display: flex;
  flex-direction: column;
  gap: 11px;
  min-width: 0;
}
.track {
  display: grid;
  grid-template-columns: 40px 1fr 58px;
  gap: 11px;
  align-items: center;
}
.track .name {
  font-size: 11px;
  color: var(--tv-text-muted);
  font-weight: 650;
}
.rail {
  position: relative;
  height: 22px;
  border-radius: 4px;
  background: var(--tv-bg-inset);
  border: 1px solid var(--tv-border);
}
.zone {
  position: absolute;
  top: 0;
  bottom: 0;
}
.zone.hi {
  right: 0;
  background: var(--tv-up-bg);
  border-radius: 0 3px 3px 0;
}
.zone.lo {
  left: 0;
  background: var(--tv-down-bg);
  border-radius: 3px 0 0 3px;
}
.mark {
  position: absolute;
  top: 50%;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
  border: 2px solid var(--tv-bg-card);
  background: var(--tv-text-muted);
  box-shadow: 0 0 0 1px var(--tv-border-strong);
}
.mark.high {
  background: var(--tv-up);
  box-shadow: 0 0 0 1px var(--tv-up);
}
.mark.low {
  background: var(--tv-down);
  box-shadow: 0 0 0 1px var(--tv-down);
}
.ends {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--tv-text-muted);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.absent {
  font-size: 11px;
  color: var(--tv-text-muted);
}
.pct {
  text-align: right;
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--tv-text-secondary);
}
.pct.high {
  color: var(--tv-up);
}
.pct.low {
  color: var(--tv-down);
}

.tail {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.row-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.history-pager {
  margin: 4px 0 20px;
  justify-content: center;
}
/* 手工产业备注:统计旁边的「为什么」。整块可点进编辑。 */
.note {
  max-width: 300px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tv-text-secondary);
  background: color-mix(in srgb, var(--tv-warn) 8%, transparent);
  border-left: 2px solid var(--tv-warn);
  border-radius: 4px;
  padding: 4px 8px;
  text-align: left;
  white-space: pre-wrap;
  cursor: pointer;
}
.note:hover {
  background: color-mix(in srgb, var(--tv-warn) 14%, transparent);
}
/* 现货基差背景条:比统计弱一档,它是背景不是信号。 */
.basis {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
  font-size: 12px;
  color: var(--tv-text-secondary);
}
.basis .k {
  color: var(--tv-text-muted);
}
.basis .v {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.basis .v.disc {
  color: var(--tv-up);
}
.basis .v.prem {
  color: var(--tv-down);
}
.basis .pctile {
  color: var(--tv-text-muted);
}
/* 资金流向条与基差条同一档(都是背景),叠在一起时留一点行距区分层次。
   颜色沿用涨跌两色:这里的「涨跌」指价差本身走扩/收窄,与基差同义,不是好坏。 */
.basis.fund {
  margin-top: 2px;
}
.basis .flow-tip {
  opacity: 0.75;
}
.note-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--tv-text-secondary);
}

/* 页头的提醒段：贴到极值不等于会回归。 */
.caveat {
  color: var(--tv-text-secondary);
  border-left: 3px solid var(--tv-warn);
  padding-left: 12px;
  margin-top: 10px;
}
.caveat strong {
  color: var(--tv-text);
}

.state-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
/* 「✓ 合格」:分层规则第一层通过。用主色蓝,与涨跌红绿、警示橙都区分开——
   它是「值得做」,不是方向也不是警告。 */
.badge-q {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-blue);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-blue-bg);
  color: var(--tv-blue);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}
.tally .v.qual {
  color: var(--tv-blue);
}
.tally .v.entry {
  color: var(--tv-blue);
}
/* 「⚡ 反弹进场」(生猪,DEC-121):实心橙,与正规 ⚡ 的蓝分开 —— 两者出场口径不同。 */
.badge-entry.bounce {
  background: var(--el-color-warning);
}
/* 「⚡ 进场」:全页唯一的实心蓝——它是唯一的行动指令,必须一眼跳出来。 */
.badge-entry {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: var(--tv-radius-sm);
  background: var(--tv-blue);
  color: var(--el-color-white);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}

/* 「临近交割」:灰底——不是机会也不是警报,是纪律性退出区。 */
.badge-redline {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-border-strong);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-bg-inset);
  color: var(--tv-text-muted);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}
.badge-decay {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px dashed var(--tv-border-strong);
  border-radius: var(--tv-radius-sm);
  color: var(--tv-text-muted);
  font-size: 11px;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}
.revert .basis.mae {
  color: var(--tv-text-secondary);
}

/* 「⚠ 信号差」:拐头反复。红色系但用描边不用实底——它是警示不是方向,
   也不能比 ⚡ 更抢眼。 */
.badge-choppy {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-up);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-up-bg);
  color: var(--tv-up);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}

/* 「新」徽标：前一交易日按同一阈值还没触发。
   用警示橙而不是红绿——红绿在全站是涨跌语义，借过来会被读成方向。 */
.badge-new {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-warn);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-warn-bg);
  color: var(--tv-warn);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}

/* 平台位(DEC-095)。摘要一行常驻,阶梯折叠。
   注意折叠元素 height:0 时 padding/border 仍占位,所以收起态不给它们。 */
.shelf { margin-top: 6px; font-size: 12px; }
.shelf > summary {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  justify-content: flex-end; cursor: pointer; list-style: none;
}
.shelf > summary::-webkit-details-marker { display: none; }
.shelf > summary .k { color: var(--tv-text-muted); }
.shelf > summary .more { color: var(--tv-text-muted); text-decoration: underline dotted; }
.shelf[open] > summary .more::after { content: ' ▲'; }
.shelf:not([open]) > summary .more::after { content: ' ▼'; }
.shelf .chip {
  padding: 1px 7px; border-radius: 3px; font-style: normal; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.shelf .chip.stop { color: var(--tv-warn, #d98e00); border: 1px solid currentColor; }
.shelf .chip.target { color: var(--tv-accent, #2563d9); border: 1px solid currentColor; }
.shelf .chip em { font-style: normal; font-weight: 600; margin-left: 3px; }
.shelf .ladder { margin-top: 6px; border-top: 1px solid var(--tv-border, #e3e8ee); padding-top: 6px; }
/* 概率不可用时的说明。**不能只留一列「—」**——那看着像数据没采到。 */
.shelf .reach-na {
  margin: 6px 0 0; padding: 5px 7px; font-size: 12px; line-height: 1.5;
  color: var(--tv-text-muted); background: var(--tv-fill-muted, #f6f8fa);
  border-radius: 4px;
}
.shelf .rung {
  display: grid; grid-template-columns: 84px 1fr 44px; gap: 8px;
  align-items: center; padding: 3px 0;
}
.shelf .rung .lvl { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
.shelf .rung.above .lvl { color: var(--tv-text-secondary); }
.shelf .rung .meta { color: var(--tv-text-muted); display: flex; gap: 6px; align-items: center; }
.shelf .rung .meta b { color: var(--tv-text-secondary); font-weight: 600; }
.shelf .rung .pct { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
.shelf .shelf-rule {
  margin: 6px 0 0; color: var(--tv-text-muted); line-height: 1.7; text-align: left;
}
.shelf .q { text-decoration: underline dotted; cursor: help; margin: 0 6px; }
.revert .legacy { text-decoration: underline dotted; cursor: help; }
/* 双向盈亏比。信号指的那一侧加底色,另一侧是对照——它不是建议。 */
.shelf .dir { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.shelf .dir th {
  text-align: right; font-weight: 400; color: var(--tv-text-muted);
  padding: 2px 6px; border-bottom: 1px solid var(--tv-border, #e3e8ee);
}
.shelf .dir th:first-child { text-align: left; }
.shelf .dir td { text-align: right; padding: 3px 6px; font-variant-numeric: tabular-nums; }
.shelf .dir td:first-child { text-align: left; }
.shelf .dir tr.signal { background: var(--tv-accent-soft, #eef4ff); }
.shelf .dir i { font-style: normal; color: var(--tv-text-muted); margin-left: 4px; }
.shelf .dir b { margin-left: 4px; }
/* 盈亏比 <1:赚的没有亏的多。用警示色,不用红绿——那是涨跌语义。 */
.shelf .dir td.thin { color: var(--tv-warn, #d98e00); font-weight: 700; }
.shelf .dir td.none { color: var(--tv-text-muted); text-align: left; }

/* 另一侧统计:比主统计再弱一档,它是对照不是结论。 */
.revert.alt {
  margin-top: 2px;
  opacity: 0.8;
}
/* 历史回归率。同样避开红绿：这是个概率，不是价格方向。
   强弱只用主色与灰色的深浅区分。 */
/* 原来是竖排:上面顶着那个大号「曾回归 100%」,下面几行小字。
   DEC-098 把大号数字撤掉之后,剩下三个短标签各占一行,右栏空得发慌、
   「历年点数 ⓘ」还成了孤岛。改成一行排布,中间用「·」分隔。 */
.revert {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: baseline;
  gap: 2px 8px;
  cursor: help;
}
.revert > .basis + .basis::before {
  content: '·';
  margin-right: 8px;
  color: var(--tv-text-muted);
}
.revert .basis {
  font-size: 11px;
  line-height: 1.3;
  text-align: right;
  color: var(--tv-text-muted);
  font-variant-numeric: tabular-nums;
}
/* 「持到期」单独上色：它为负说明历年这段最终朝反方向走，是回归率骗人的地方。
   仍然避开红绿——这不是价格方向，是统计倾向。 */
.revert .basis em {
  font-style: normal;
  font-weight: 700;
}
.revert .basis em.with {
  color: var(--tv-blue);
}
.revert .basis em.against {
  color: var(--tv-warn);
}
.revert.absent {
  font-size: 11px;
  color: var(--tv-text-muted);
  cursor: default;
}

@media (max-width: 880px) {
  .row {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .tail {
    flex-direction: row;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 14px;
  }
  /* 窄屏下 .tail 横过来了，回归率跟着改成左对齐横排，
     否则右对齐的两行文字会吊在按钮中间。 */
  .revert {
    flex-direction: row;
    align-items: baseline;
    gap: 6px;
  }
  .revert .basis {
    text-align: left;
  }
}
</style>
