<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { addChange, sideDelta, signedChange } from '../seatChange'
import { offBoardBands } from '../offBoard'
import { ElMessage } from 'element-plus'
import type { CandlestickSeriesOption, EChartsOption } from 'echarts'
import {
  getSeatBuilding,
  getSeatPositions,
  getSpreadVarieties,
  type BuildingDay,
  type SeatBuildingResponse,
  type SeatPositionRow
} from '../api'
import SpreadChart from '../components/SpreadChart.vue'
import { lastYearStartIndex } from '../spreadCharts'

// 选过的席位、品种、合约记在本地，刷新、关标签页、明天再来都还在，直到下次主动改选。
// 运营者盯的通常就是那么几家机构的那么一两个品种，每次进来重选一遍是纯粹的重复劳动。
// 日期不记：数据每天在长，记住某个旧日期只会让人看到过期的表还以为是最新的。
const STORE_KEYS = {
  member: 'seats.member',
  instrument: 'seats.instrument',
  contract: 'seats.contract'
} as const

function remembered(key: keyof typeof STORE_KEYS) {
  try {
    return localStorage.getItem(STORE_KEYS[key]) ?? ''
  } catch {
    // 隐私模式下 localStorage 会抛异常。记不住是小事，页面打不开是大事。
    return ''
  }
}

function remember(key: keyof typeof STORE_KEYS, value: string) {
  try {
    localStorage.setItem(STORE_KEYS[key], value)
  } catch {
    // 同上：存不住就算了，不影响用。
  }
}

// 席位与日期由两个子页共用：先选好一次，切标签不用重选。
const member = ref(remembered('member'))
const tradeDate = ref('')
const members = ref<string[]>([])
const availableDates = ref<string[]>([])
// 日历上只让点有数据的交易日。比较用本地时区的年月日拼串,不能用 toISOString——
// 它按 UTC 取日期,东八区晚上会差一天。
const availableDateSet = computed(() => new Set(availableDates.value))
function isNotTradingDay(day: Date) {
  const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
  return !availableDateSet.value.has(key)
}
const tab = ref<'positions' | 'building'>('positions')

// 席位持仓
const rows = ref<SeatPositionRow[]>([])
const instrumentFilter = ref<string[]>([])
const loadingPositions = ref(false)

// 建仓过程
const buildingInstrument = ref(remembered('instrument'))
const buildingContract = ref(remembered('contract'))
// 合约选择器的选项由接口给,不再从「所选交易日当天的持仓行」推导。
// 旧写法只列得出当天还在榜的两三个合约:换个日子就少几个选项,等于把
// 「今天在榜」误当成「存在过」——运营者选了高盛后挑不到 AU2608,就是这个。
const buildingContracts = ref<string[]>([])
const days = ref<BuildingDay[]>([])
const multiplier = ref<string | null>(null)
// 汇总档 K 线的口径。单合约档为 null（那是真实行情，没有口径可言）。
const priceSeriesKind = ref<SeatBuildingResponse['price_series_kind']>(null)
const loadingBuilding = ref(false)

// 品种的中文名。库里 product_instrument_scope 定了它，那张表也是套利页品种下拉的
// 依据——两个页面显示同一个名字，不各写一份。取不到就退回代码，宁可少个中文名，
// 也不要因为一次取名失败让整张表打不开。
const varietyNames = ref<Record<string, string>>({})

/** 「苹果 AP」而不是光秃秃的「AP」。运营者看的是品种，代码只是它的编号。 */
function varietyLabel(code: string) {
  const name = varietyNames.value[code]
  return name && name !== code ? `${name} ${code}` : code
}

watch(member, (value) => {
  if (value) remember('member', value)
})
watch(buildingInstrument, (value) => {
  if (value) remember('instrument', value)
})
// 合约要连空值一起记：空就是「合约汇总」这个选择本身。只在非空时记的话，
// 他主动切回汇总，下次进来又会被翻出上一个合约。
watch(buildingContract, (value) => remember('contract', value))

const route = useRoute()
const router = useRouter()

async function loadPositions() {
  loadingPositions.value = true
  try {
    const { data } = await getSeatPositions({
      member: member.value || undefined,
      tradeDate: tradeDate.value || undefined
    })
    members.value = data.members
    availableDates.value = data.available_dates
    rows.value = data.rows
    if (data.trade_date) tradeDate.value = data.trade_date
    // 没选会员，或记住的那个已经不在名录里（机构改名、退市、数据源换写法），
    // 都退回名录第一个——否则页面停在一张永远空的表上，看不出是「没数据」还是「坏了」。
    const missing = Boolean(member.value) && !data.members.includes(member.value)
    if ((!member.value || missing) && data.members.length) {
      if (missing) ElMessage.info(`上次选的「${member.value}」已不在名录，改为 ${data.members[0]}`)
      member.value = data.members[0]
      await loadPositions()
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '席位持仓读取失败')
    rows.value = []
  } finally {
    loadingPositions.value = false
  }
}

async function loadBuilding() {
  if (!member.value || !buildingInstrument.value) {
    days.value = []
    buildingContracts.value = []
    return
  }
  loadingBuilding.value = true
  try {
    const { data } = await getSeatBuilding(
      buildingInstrument.value,
      member.value,
      buildingContract.value || undefined
    )
    multiplier.value = data.price_multiplier
    days.value = data.days
    buildingContracts.value = data.contracts
    priceSeriesKind.value = data.price_series_kind
    // 上次记住的合约可能已经到期了——期货合约会到期，这是常态不是异常。
    // contracts 是该席位在这个品种上历史持有过的全部合约，不在里面就没有可看的
    // 东西，退回合约汇总。留在那里只会显示一张空表，看上去像数据坏了。
    if (buildingContract.value && !data.contracts.includes(buildingContract.value)) {
      buildingContract.value = '' // 触发 watch 重新取一次汇总档
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '建仓过程读取失败')
    days.value = []
  } finally {
    loadingBuilding.value = false
  }
}

async function loadVarietyNames() {
  try {
    const { data } = await getSpreadVarieties('self')
    varietyNames.value = Object.fromEntries(data.items.map((item) => [item.symbol, item.name]))
  } catch {
    // 只是个显示名。取不到就退回代码，不打断这个页面——这里报错会盖住真正要看的表。
  }
}

onMounted(() => {
  if (route.query.tab === 'building') tab.value = 'building'
  loadVarietyNames()
  loadPositions()
})
watch([member, tradeDate], () => {
  loadPositions()
  if (tab.value === 'building') loadBuilding()
})
watch(tab, (value) => {
  router.replace({ query: { ...route.query, tab: value } })
  if (value === 'building') loadBuilding()
})
watch([buildingInstrument, buildingContract], () => loadBuilding())

/** 该会员当天持有的品种，用于「筛选商品显示」。 */
const instruments = computed(() =>
  [...new Set(rows.value.map((row) => row.instrument))].sort()
)

/**
 * 建仓过程的品种选项：当天在榜的品种，外加当前已选的那个。
 *
 * 上次记住的品种今天可能不在榜（他那天没进前二十）。只列在榜品种的话，选择器会
 * 显示一片空白，而下面的图明明画着那个品种的数据——看上去像选择器坏了。
 * 建仓过程本来也不受所选交易日限制：不在榜不等于没有建仓过程可看。
 */
const buildingInstrumentOptions = computed(() => {
  const list = new Set(instruments.value)
  if (buildingInstrument.value) list.add(buildingInstrument.value)
  return [...list].sort()
})

const num = (value: string | null) => (value === null || value === '' ? null : Number(value))

/** 按品种分组，每个品种下按合约列出多空持仓——三禾那张表的形状。 */
interface ContractLine {
  contract: string
  long: number
  longChange: number | null | undefined
  short: number
  shortChange: number | null | undefined
}
interface InstrumentBlock {
  instrument: string
  netTotal: number
  netChange: number | null | undefined
  contracts: ContractLine[]
}

// 增减量三态（未知 / 无行 / 真值）与吸收律见 src/seatChange.ts，那里有测试盯着。

const blocks = computed<InstrumentBlock[]>(() => {
  const wanted = new Set(instrumentFilter.value)
  const byInstrument = new Map<string, Map<string, ContractLine>>()
  for (const row of rows.value) {
    if (row.is_variety_total || !row.contract) continue
    if (wanted.size && !wanted.has(row.instrument)) continue
    if (row.rank_type === 'volume') continue
    const contracts = byInstrument.get(row.instrument) ?? new Map()
    const line = contracts.get(row.contract) ?? {
      contract: row.contract,
      long: 0,
      longChange: undefined,
      short: 0,
      shortChange: undefined
    }
    const quantity = Number(row.quantity)
    const change = num(row.change)
    if (row.rank_type === 'long') {
      line.long += quantity
      line.longChange = addChange(line.longChange, change)
    } else {
      line.short += quantity
      line.shortChange = addChange(line.shortChange, change)
    }
    contracts.set(row.contract, line)
    byInstrument.set(row.instrument, contracts)
  }
  return [...byInstrument.entries()]
    .map(([instrument, contracts]) => {
      const lines = [...contracts.values()].sort((a, b) => a.contract.localeCompare(b.contract))
      return {
        instrument,
        netTotal: lines.reduce((sum, line) => sum + line.long - line.short, 0),
        // 净变化 = Σ多头增减 − Σ空头增减，同样遵吸收律：任一合约的任一侧
        // **有行但增减量未知**，整个品种的净变化就是未知。
        // 一侧根本没有行（undefined）不算未知，它就是 0——某合约只上了空头榜，
        // 不代表它的多头变化不可知。
        netChange: lines.reduce<number | null | undefined>((sum, line) => {
          const long = sideDelta(line.longChange)
          const short = sideDelta(line.shortChange)
          return addChange(addChange(sum, long), short === null ? null : -short)
        }, undefined),
        contracts: lines
      }
    })
    .sort((a, b) => a.instrument.localeCompare(b.instrument))
})

function openBuilding(instrument: string, contract?: string) {
  buildingInstrument.value = instrument
  buildingContract.value = contract ?? ''
  tab.value = 'building'
}

const signed = signedChange
const fmt = (value: number) => value.toLocaleString('zh-CN')

// —— 建仓过程的三联图 ——
const dates = computed(() => days.value.map((day) => day.trade_date))
const netSeries = computed(() => days.value.map((day) => num(day.net_position)))
const pnlSeries = computed(() => days.value.map((day) => num(day.daily_pnl)))
const cumulativeSeries = computed(() => days.value.map((day) => num(day.cumulative_pnl)))

// 国内看盘的惯例：红涨绿跌。盈亏柱按正负着色，一眼能看出哪天在赚。
const UP = '#c0392b'
const DOWN = '#27ae60'
function pnlBars(values: Array<number | null>) {
  return values.map((value) => ({
    value,
    itemStyle: { color: (value ?? 0) >= 0 ? UP : DOWN }
  }))
}

/** 金额按万/亿收敛，否则纵轴挤满零看不清量级。 */
function money(value: number) {
  const abs = Math.abs(value)
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(0)} 万`
  return value.toFixed(0)
}
const candles = computed(() =>
  days.value.map((day) => {
    const open = num(day.open_price)
    const close = num(day.close_price)
    const low = num(day.low_price)
    const high = num(day.high_price)
    // 缺一项就整根不画。'-' 是 ECharts 自己的空值写法。
    return open === null || close === null || low === null || high === null
      ? '-'
      : [open, close, low, high]
  })
)
const hasCandles = computed(() => candles.value.some((item) => item !== '-'))
/** 成本不完整的天数。汇总档看的是均价覆盖了多少手，不是「有没有一个 cost 字段」。 */
const gapDays = computed(() =>
  days.value.filter((day) =>
    day.legs
      ? Number(day.legs.long_cost_lots) < Number(day.legs.long_lots)
        || Number(day.legs.short_cost_lots) < Number(day.legs.short_lots)
      : day.cost === null
  ).length
)

// —— 掉榜区间 ——
//
// 交易所只发前 20 名。该席位掉出榜单的那些天，官方文件里没有他这一行——那是
// 「不知道」，不是「零」。曲线在这里必须断开（connectNulls: false 已经做到），
// 但光断开看不出是缺数据还是真平仓，所以把这些天用底色圈出来并写明原因。
const offBoardDays = computed(
  () => days.value.filter((day) => day.net_position === null).length
)

// 区间合并在 offBoard.ts 里，那边有测试：差一格就会把回榜那天也涂成空白。
const bands = computed(() =>
  offBoardBands(
    days.value.map((day) => ({ trade_date: day.trade_date, known: day.net_position !== null }))
  )
)

const offBoardMark = computed(() => ({
  silent: true,
  itemStyle: { color: 'rgba(230, 162, 60, 0.16)' },
  label: { show: false },
  data: bands.value
}))

// —— 小窗 ——
//
// 四张图共用同一段正文：同一天在哪张图上停住，看到的都该是同一组数。各图自己再
// 按需要补一两行（K 线图补开高低收）。正文直接读 days[i]，不靠 ECharts 传进来的
// series 值——K 线图上的成本线已经撤了（运营者要的是图上干净、数还在手边），
// 靠 series 就取不到它。
const lots = (value: number) => `${value.toLocaleString('zh-CN')} 手`
const price = (value: number) => value.toFixed(2)

/** 一行「标签 + 值」。值可着色：多单红、空单绿，国内看盘的惯例。 */
function row(label: string, value: string, color?: string) {
  const painted = color ? `<span style="color:${color};font-weight:600">${value}</span>` : value
  return `<div style="display:flex;gap:12px;justify-content:space-between"><span>${label}</span>${painted}</div>`
}

/** 该腿的均价。覆盖不全时说明覆盖了多少手，不让人当成全部持仓的成本。 */
function legCost(cost: string | null, costLots: string, allLots: string) {
  if (cost === null) return '成本不可知'
  const covered = Number(costLots)
  const total = Number(allLots)
  return covered < total ? `${price(Number(cost))}（覆盖 ${lots(covered)}）` : price(Number(cost))
}

function tooltipBody(index: number, head: string[] = []) {
  const day = days.value[index]
  if (!day) return ''
  const parts = [`<div style="margin-bottom:4px"><b>${day.trade_date}</b></div>`, ...head]

  if (day.legs) {
    // 品种汇总：合约按净方向分两组。净多的那几个是「多单」，净空的是「空单」，
    // 两者相减才是净持仓——这是运营者定的口径。
    const long = Number(day.legs.long_lots)
    const short = Number(day.legs.short_lots)
    if (long > 0) {
      parts.push(row('多单', lots(long), UP))
      parts.push(row('　净持仓成本（推算）',
        legCost(day.legs.long_cost, day.legs.long_cost_lots, day.legs.long_lots)))
    }
    if (short > 0) {
      parts.push(row('空单', lots(short), DOWN))
      parts.push(row('　净持仓成本（推算）',
        legCost(day.legs.short_cost, day.legs.short_cost_lots, day.legs.short_lots)))
    }
  }

  const net = num(day.net_position)
  if (net === null) {
    parts.push(row('净持仓', '掉出前 20 · 未知'))
  } else {
    parts.push(row('净持仓', lots(Math.abs(net)) + (net === 0 ? '' : net > 0 ? '（净多）' : '（净空）'),
      net === 0 ? undefined : net > 0 ? UP : DOWN))
  }
  // 单合约档的成本。汇总档已经在上面按两腿分别列过了。
  if (!day.legs) {
    parts.push(row('净持仓成本（推算）', day.cost === null ? '不可知' : price(Number(day.cost))))
  }

  // 「估计」二字不能省：这是由公开持仓与结算价推出来的，不是他的对账单。
  // 当日盈亏运营者明确说不用，只留累计。
  const cumulative = num(day.cumulative_pnl)
  if (cumulative !== null) {
    parts.push(row('估计累计盈利',
      `${cumulative >= 0 ? '+' : '−'}${money(Math.abs(cumulative))}`,
      cumulative >= 0 ? UP : DOWN))
  }
  return parts.join('')
}

/**
 * 最新一天的摘要，常驻在「净持仓」标题旁边。
 *
 * 与小窗同源、同口径，只是不必悬停就能看到——运营者要的是「点进来一眼知道
 * 现在什么情况」。**不含盈亏**：他明确说了盈利不用显示在这里。
 */
const latest = computed(() => {
  const day = [...days.value].reverse().find((item) => item.net_position !== null)
  if (!day) return null
  const net = Number(day.net_position)
  const parts: Array<{ text: string; tone?: 'up' | 'down' }> = []
  if (day.legs) {
    const long = Number(day.legs.long_lots)
    const short = Number(day.legs.short_lots)
    if (long > 0) {
      parts.push({ text: `多单 ${lots(long)}`, tone: 'up' })
      parts.push({
        text: `均价 ${legCost(day.legs.long_cost, day.legs.long_cost_lots, day.legs.long_lots)}`
      })
    }
    if (short > 0) {
      parts.push({ text: `空单 ${lots(short)}`, tone: 'down' })
      parts.push({
        text: `均价 ${legCost(day.legs.short_cost, day.legs.short_cost_lots, day.legs.short_lots)}`
      })
    }
  }
  parts.push({
    text: `净持仓 ${lots(Math.abs(net))}${net === 0 ? '' : net > 0 ? '（净多）' : '（净空）'}`,
    tone: net === 0 ? undefined : net > 0 ? 'up' : 'down'
  })
  if (!day.legs) {
    parts.push({
      text: `净持仓成本（推算） ${day.cost === null ? '不可知' : price(Number(day.cost))}`
    })
  }
  return { date: day.trade_date, parts }
})

/**
 * 汇总档 K 线的口径说明，摆在「行情」标题旁边。
 *
 * 这根 K 线是算出来的，不是任何一个合约的真实成交价——不写明，看的人会拿这个价位
 * 去定止损。加权那句不提「指数」二字：市面上的指数各家算法不同，说了反而像在对标。
 */
const priceSeriesNote = computed(() => {
  switch (priceSeriesKind.value) {
    case 'open_interest_weighted':
      return '按持仓量加权 · 合成价'
    case 'dominant_unadjusted':
      return '主力连续 · 不复权，换月处有跳空'
    default:
      return null
  }
})

/** ECharts 把 axis 小窗的参数传成数组；取哪一条都行，要的只是那天的下标。 */
function axisIndex(params: unknown) {
  const first = Array.isArray(params) ? params[0] : params
  const index = (first as { dataIndex?: number } | undefined)?.dataIndex
  return typeof index === 'number' ? index : null
}

const tooltip = {
  trigger: 'axis' as const,
  formatter: (params: unknown) => {
    const index = axisIndex(params)
    return index === null ? '' : tooltipBody(index)
  }
}

// 底部滑钮，与价差走势图同一套。十八年的日线挤在一屏里只看得出个大概形状，
// 想看某一段建仓就得能拉。
const zoom = computed(() => [
  // 滚轮不给 dataZoom 用：这一页四张图竖着叠，滚轮被图抢走就翻不动页面了。
  // 缩放交给滑钮，图内按住拖动仍可平移。
  {
    type: 'inside' as const,
    zoomOnMouseWheel: false,
    moveOnMouseWheel: false,
    // 默认落在最近一年，见 lastYearStartIndex。
    startValue: lastYearStartIndex(dates.value),
    endValue: Math.max(dates.value.length - 1, 0)
  },
  {
    type: 'slider' as const,
    startValue: lastYearStartIndex(dates.value),
    endValue: Math.max(dates.value.length - 1, 0),
    height: 26,
    bottom: 8,
    borderColor: '#e2e1dd',
    fillerColor: 'rgba(120,150,200,0.12)',
    handleStyle: { color: '#b9b8b4' },
    dataBackground: {
      lineStyle: { color: '#c9c8c4' },
      areaStyle: { color: '#eeeeec' }
    },
    labelFormatter: (value: number) => dates.value[Math.round(value)] ?? ''
  }
])
/** 留给滑钮的高度。忘了加就是滑钮压在横轴标签上。 */
const GRID_BOTTOM = 62

const priceOption = computed<EChartsOption>(() => ({
  grid: { left: 60, right: 24, top: 24, bottom: GRID_BOTTOM },
  dataZoom: zoom.value,
  // K 线图上原来还有一条成本蓝线，运营者要求撤掉：图上只留行情，成本进小窗。
  // 数一个没少，见 tooltipBody。
  tooltip: {
    ...tooltip,
    formatter: (params: unknown) => {
      const index = axisIndex(params)
      if (index === null) return ''
      // ECharts 的 K 线原样是 [开, 收, 低, 高]，别按图上的高低顺序读。
      // 四项各占一行：挤成「开盘 / 收盘  955.82 / 943.16」要读的人自己在心里
      // 把两个数配回两个标签，配错一次就看反了当天的涨跌。
      const bar = candles.value[index]
      const head = Array.isArray(bar)
        ? [
            row('开盘', price(bar[0])),
            row('收盘', price(bar[1])),
            row('最低', price(bar[2])),
            row('最高', price(bar[3]))
          ]
        : [row('行情', '当日无 K 线')]
      return tooltipBody(index, head)
    }
  },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [
    {
      name: 'K线',
      type: 'candlestick' as const,
      data: candles.value as unknown as CandlestickSeriesOption['data'],
      // 掉榜区间的底色原先挂在成本线上，成本线撤了就得挪过来，否则整段标注消失。
      markArea: offBoardMark.value
    }
  ]
}))
const netOption = computed<EChartsOption>(() => ({
  grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
  dataZoom: zoom.value,
  tooltip,
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  // scale 不能开：净持仓要看得出离零轴多远，多空翻向也全靠零轴分界。
  yAxis: { type: 'value' as const },
  series: [
    {
      name: '净持仓',
      type: 'line' as const,
      data: netSeries.value,
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 2 },
      // 掉榜区间的底色。断开加底色，才分得清「缺数据」和「真的平了」。
      markArea: offBoardMark.value,
      // 零轴：正的是净多、负的是净空，没有这条线读不出方向。
      markLine: {
        silent: true,
        symbol: 'none',
        data: [{ yAxis: 0 }],
        lineStyle: { color: '#999', type: 'dashed' as const },
        label: { show: false }
      }
    }
  ]
}))
const pnlOption = computed<EChartsOption>(() => ({
  grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
  dataZoom: zoom.value,
  tooltip,
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, axisLabel: { formatter: money } },
  series: [{ name: '当日盈亏', type: 'bar' as const, data: pnlBars(pnlSeries.value) }]
}))
const cumulativeOption = computed<EChartsOption>(() => ({
  grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
  dataZoom: zoom.value,
  tooltip,
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, axisLabel: { formatter: money } },
  series: [
    { name: '合约累计盈亏', type: 'bar' as const, data: pnlBars(cumulativeSeries.value) }
  ]
}))
/** 末日累计值，放在标题旁边——图能看趋势，数字才好念。 */
const cumulativeTotal = computed(() => {
  const last = cumulativeSeries.value.filter((value) => value !== null).pop()
  return last === undefined || last === null ? null : last
})

</script>

<template>
  <section class="seats">
    <header class="page-head">
      <h1>席位</h1>
      <p>选一个会员和一个交易日，两个子页共用这组选择。</p>
    </header>

    <el-card shadow="never" class="shared">
      <div class="control-row">
        <el-select
          v-model="member"
          style="width: 220px"
          filterable
          placeholder="选择席位"
          :disabled="loadingPositions"
        >
          <el-option v-for="name in members" :key="name" :label="name" :value="name" />
        </el-select>
        <el-date-picker
          v-model="tradeDate"
          type="date"
          style="width: 180px"
          placeholder="交易日"
          value-format="YYYY-MM-DD"
          :clearable="false"
          :disabled-date="isNotTradingDay"
          :disabled="loadingPositions || !availableDates.length"
        />
      </div>
      <el-radio-group v-model="tab" class="tabs">
        <el-radio-button value="positions">席位持仓</el-radio-button>
        <el-radio-button value="building">建仓过程</el-radio-button>
      </el-radio-group>
    </el-card>

    <template v-if="tab === 'positions'">
      <el-card shadow="never">
        <template #header>
          <div class="panel-head">
            <h2>{{ tradeDate }} {{ member }} 席位持仓</h2>
            <el-select
              v-model="instrumentFilter"
              multiple
              collapse-tags
              clearable
              placeholder="筛选商品显示"
              style="width: 260px"
            >
              <el-option
                v-for="code in instruments"
                :key="code"
                :label="varietyLabel(code)"
                :value="code"
              />
            </el-select>
          </div>
        </template>
        <el-empty v-if="!blocks.length" description="这一天该席位没有持仓" />
        <table v-else class="positions">
          <thead>
            <tr>
              <th>品种</th>
              <th>总净持仓</th>
              <th>合约</th>
              <th>多头持仓</th>
              <th>空头持仓</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="block in blocks" :key="block.instrument">
              <tr v-for="(line, index) in block.contracts" :key="line.contract">
                <td v-if="index === 0" :rowspan="block.contracts.length" class="instrument">
                  {{ varietyLabel(block.instrument) }}
                </td>
                <td v-if="index === 0" :rowspan="block.contracts.length" class="net">
                  <div :class="block.netTotal >= 0 ? 'long' : 'short'">
                    {{ block.netTotal >= 0 ? '净多' : '净空' }}{{ fmt(Math.abs(block.netTotal)) }}
                  </div>
                  <div class="change">{{ signed(block.netChange) }}</div>
                  <el-button size="small" @click="openBuilding(block.instrument)">
                    品种汇总建仓过程
                  </el-button>
                </td>
                <td class="contract">
                  <div>{{ line.contract }}</div>
                  <el-button
                    size="small"
                    @click="openBuilding(block.instrument, line.contract)"
                  >
                    建仓过程
                  </el-button>
                </td>
                <td class="figure">
                  <div>{{ fmt(line.long) }}</div>
                  <div class="change">{{ signed(line.longChange) }}</div>
                </td>
                <td class="figure">
                  <div>{{ fmt(line.short) }}</div>
                  <div class="change">{{ signed(line.shortChange) }}</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </el-card>
    </template>

    <template v-else>
      <el-card shadow="never">
        <div class="control-row">
          <el-select
            v-model="buildingInstrument"
            style="width: 150px"
            placeholder="选择品种"
            :disabled="loadingBuilding"
            @change="buildingContract = ''"
          >
            <el-option
              v-for="code in buildingInstrumentOptions"
              :key="code"
              :label="varietyLabel(code)"
              :value="code"
            />
          </el-select>
          <el-select
            v-model="buildingContract"
            style="width: 220px"
            placeholder="合约汇总（全部合约）"
            :disabled="loadingBuilding"
          >
            <!-- 汇总要有一个点得到的选项。原先只能靠清空按钮回到汇总档，
                 那等于把一个主要视角藏在一个 × 后面。 -->
            <el-option label="合约汇总（全部合约）" value="" />
            <el-option v-for="code in buildingContracts" :key="code" :label="code" :value="code" />
          </el-select>
          <span class="hint">
            {{ buildingContract ? '单合约' : '合约汇总' }}
            <template v-if="multiplier">· 点值 {{ Number(multiplier) }}</template>
          </span>
        </div>
        <p class="note">
          成本按<strong>结算价</strong>推算：加仓加权平均、减仓不改均价、净头寸翻向时重置。
          字段名是「净持仓成本（推算）」而不是成交均价——我们看不到成交明细。
          <template v-if="gapDays">
            其中 <strong>{{ gapDays }}</strong> 天成本不可知（掉出前 20 或当日无结算价），图上断开显示。
          </template>
        </p>
        <p v-if="!buildingContract" class="note">
          合约汇总把该席位在这个品种<strong>各个合约上的持仓逐一算完再相加</strong>：净多的那些
          合约合成「多单」、净空的合成「空单」，两者相减才是净持仓，均价各按手数加权。
          小窗里能看到这三个数。
          <br />
          用的是逐合约榜而不是交易所的品种汇总榜——后者只有一个总手数，推不出成本、也分不出
          两腿。代价是他持有、却排不进那个合约前二十的零头看不到：永安黄金 3316 个交易日实测，
          两者完全相等 2722 天，平均差 56 手。
        </p>
        <p v-if="offBoardDays" class="note off-board">
          <span class="swatch" aria-hidden="true"></span>
          <span>
            <strong>{{ offBoardDays }}</strong> 天该席位掉出交易所前 20 榜，
            <strong>持仓未知</strong>——不是清仓。交易所只公布前 20 名，那些天文件里没有他这一行。
            图上以此底色标出并断开曲线；成本与累计盈亏在这几天原地保留，回榜后接着算。
          </span>
        </p>
      </el-card>

      <el-empty v-if="!loadingBuilding && !days.length" description="选一个品种，或该席位在此品种上没有持仓" />
      <template v-else>
        <el-card shadow="never">
          <!-- 原来叫「行情与成本线」。成本线已按运营者要求从图上撤掉（数字进小窗），
               标题里再留「成本线」就是说了一件图上没有的事。 -->
          <template #header>
            <div class="panel-head">
              <h2>行情</h2>
              <!-- 汇总档画的是合成价，口径必须写在图边上，不能只藏在文档里。 -->
              <span v-if="priceSeriesNote" class="series-note">{{ priceSeriesNote }}</span>
            </div>
          </template>
          <SpreadChart v-if="hasCandles" :option="priceOption" :height="320" export-name="建仓过程-行情" />
          <el-alert
            v-else
            type="info"
            :closable="false"
            title="这段时间没有行情"
            description="该品种在这些交易日上没有可用的开高低收，K 线画不出来。持仓与成本不受影响，仍在下面各图里。"
          />
        </el-card>
        <el-card shadow="never">
          <template #header>
            <!-- 最新一天的数就摆在标题旁边，不必悬停去小窗里找。 -->
            <div class="panel-head">
              <h2>净持仓</h2>
              <div v-if="latest" class="latest">
                <span class="latest-date">{{ latest.date }}</span>
                <span
                  v-for="(part, index) in latest.parts"
                  :key="index"
                  :class="part.tone"
                >{{ part.text }}</span>
              </div>
            </div>
          </template>
          <SpreadChart :option="netOption" :height="300" export-name="建仓过程-净持仓" />
        </el-card>
        <el-card shadow="never">
          <template #header><h2>当日盈亏</h2></template>
          <SpreadChart :option="pnlOption" :height="300" export-name="建仓过程-当日盈亏" />
        </el-card>
        <el-card shadow="never">
          <template #header>
            <h2>
              合约累计盈亏
              <span v-if="cumulativeTotal !== null" :class="cumulativeTotal >= 0 ? 'up' : 'down'">
                {{ cumulativeTotal >= 0 ? '累计盈利' : '累计亏损' }}
                {{ money(Math.abs(cumulativeTotal)) }}
              </span>
            </h2>
          </template>
          <SpreadChart :option="cumulativeOption" :height="300" export-name="建仓过程-累计盈亏" />
          <p class="note">
            当日盈亏的逐日累加。当日盈亏不可知的那几天（掉出前 20 或当日无结算价）按 0 计入，
            累计线不断开——断开会看起来像仓位平了。所以这是<strong>已知部分</strong>的累计。
          </p>
        </el-card>
      </template>
    </template>
  </section>
</template>

<style scoped>
.note.off-board {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.note.off-board .swatch {
  flex: none;
  width: 14px;
  height: 10px;
  border-radius: 2px;
  background: rgba(230, 162, 60, 0.16);
  border: 1px solid rgba(230, 162, 60, 0.5);
}

.up {
  color: #c0392b;
  font-weight: 600;
  margin-left: 8px;
}
.down {
  color: #27ae60;
  font-weight: 600;
  margin-left: 8px;
}
.seats {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.page-head p,
.note {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.note {
  margin-top: 12px;
  line-height: 1.7;
}
.control-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.tabs {
  margin-top: 12px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

/* K 线口径，挨着「行情」标题。比数据摘要淡一档：它是注解，不是当天的数。 */
.series-note {
  font-size: 13px;
  color: #909399;
}

/* 最新一天的摘要，挨着「净持仓」标题。窄屏换行而不是挤成一团。 */
.latest {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
  color: #606266;
}
.latest-date {
  font-weight: 600;
  color: #303133;
}
.latest .up {
  color: #c0392b;
  font-weight: 600;
  margin-left: 0;
}
.latest .down {
  color: #27ae60;
  font-weight: 600;
  margin-left: 0;
}
.seats h2 {
  margin: 0;
  font-size: 16px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.positions {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.positions th,
.positions td {
  border: 1px solid var(--el-border-color-lighter);
  padding: 8px 10px;
  text-align: center;
  vertical-align: middle;
}
.positions th {
  background: var(--el-fill-color-light);
  font-weight: 600;
}
.instrument {
  font-weight: 600;
  width: 90px;
}
.net {
  width: 170px;
}
.net .long {
  color: var(--el-color-danger);
  font-weight: 600;
}
.net .short {
  color: var(--el-color-success);
  font-weight: 600;
}
.change {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 2px 0 6px;
}
.contract {
  width: 150px;
}
.figure {
  min-width: 110px;
}
</style>
