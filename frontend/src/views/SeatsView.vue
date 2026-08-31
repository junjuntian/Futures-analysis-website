<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { addChange, sideDelta, signedChange } from '../seatChange'
import { offBoardBands, type Band } from '../offBoard'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { CandlestickSeriesOption, EChartsOption } from 'echarts'
import {
  createSeatFavorite,
  deleteSeatFavorite,
  getSeatFavorites,
  getSeatMemberInstruments,
  getSeatNetPosition,
  getSeatPnlBreakdown,
  type PnlBreakdownItem,
  getSeatPositions,
  type SeatContractCost,
  getSpreadVarieties,
  type MemberLeg,
  type NetPositionDay,
  type SeatFavorite,
  type SeatNetPositionResponse,
  type SeatPositionRow
} from '../api'
import { searchHit } from '../pinyin'
import { useAuthStore } from '../stores/auth'
import SpreadChart from '../components/SpreadChart.vue'
import { lastYearStartIndex } from '../spreadCharts'
import { chartTokens, sliderStyle, tooltipStyle } from '../chartTheme'

// 选过的席位、品种、合约记在本地，刷新、关标签页、明天再来都还在，直到下次主动改选。
// 运营者盯的通常就是那么几家机构的那么一两个品种，每次进来重选一遍是纯粹的重复劳动。
// 日期不记：数据每天在长，记住某个旧日期只会让人看到过期的表还以为是最新的。
const STORE_KEYS = {
  /** 旧键，单选时代留下的。只读不写，见 `rememberedMembers()`。 */
  member: 'seats.member',
  members: 'seats.members',
  instrument: 'seats.instrument',
  contract: 'seats.contract'
} as const

/** 一次最多合并多少家。与后端的 `MAX_NET_POSITION_MEMBERS` 是同一个数。 */
const MAX_MEMBERS = 10

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

/**
 * 读回上次选的那几家。
 *
 * 去重是必须的：本地存的值能被手工改，同一家进来两次会让「最多十家」的计数虚高，
 * 也会和后端去重后的回显对不上。
 *
 * 还要兼容单选时代的旧键——运营者本地存着 `seats.member`，直接改读新键会让他
 * 打开页面时发现选择被清空了。旧键只读不写，新键一旦写过就以新键为准。
 */
function rememberedMembers() {
  const parse = (raw: string) =>
    [...new Set(raw.split(',').filter((name) => name.length > 0))].slice(0, MAX_MEMBERS)
  const stored = remembered('members')
  if (stored) return parse(stored)
  // 旧键搬一次家。不能只是读——watch 只在值**变化**时写回，初始值原样读进来
  // 不算变化，旧键就会一直是唯一的记录，而新键永远是空的。
  const legacy = parse(remembered('member'))
  if (legacy.length) remember('members', legacy.join(','))
  return legacy
}

// 席位与日期由两个子页共用：先选好一次，切标签不用重选。
//
// 席位是**多选**：净持仓子页把勾中的这几家加总成一条曲线，席位持仓子页按家分段
// 罗列。只看一家就只勾一家——单选是多选的特例，不必为它留第二套界面。
const selected = ref<string[]>(rememberedMembers())
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
const tab = ref<'positions' | 'building' | 'pnl'>('positions')

// 席位持仓
const instrumentFilter = ref<string[]>([])
const loadingPositions = ref(false)

// 净持仓（原「建仓过程」的四张图，现在按所选那几家合计）
const buildingInstrument = ref(remembered('instrument'))
const buildingContract = ref(remembered('contract'))
// 合约选择器的选项由接口给,不再从「所选交易日当天的持仓行」推导。
// 旧写法只列得出当天还在榜的两三个合约:换个日子就少几个选项,等于把
// 「今天在榜」误当成「存在过」——运营者选了高盛后挑不到 AU2608,就是这个。
const buildingContracts = ref<string[]>([])
const days = ref<NetPositionDay[]>([])
/** 逐家×逐日两条腿(DEC-132),与 days 下标对齐,小窗逐家那几行读它。 */
const memberSeries = ref<NonNullable<SeatNetPositionResponse['member_series']>>([])
// 最新一天逐家的手数与均价，摘要下面那排用。后端连日期一起给。
const latestMembers = ref<MemberLeg[]>([])
const latestMembersDate = ref<string | null>(null)
const multiplier = ref<string | null>(null)
// 汇总档 K 线的口径。单合约档为 null（那是真实行情，没有口径可言）。
const priceSeriesKind = ref<SeatNetPositionResponse['price_series_kind']>(null)
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

// 选择连空一起记：清空也是一种选择，只在非空时记会让他下次进来被翻出旧的那几家。
watch(selected, (value) => remember('members', value.join(',')), { deep: true })
watch(buildingInstrument, (value) => {
  if (value) remember('instrument', value)
})
// 合约要连空值一起记：空就是「合约汇总」这个选择本身。只在非空时记的话，
// 他主动切回汇总，下次进来又会被翻出上一个合约。
watch(buildingContract, (value) => remember('contract', value))

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// —— 席位搜索 ——
//
// 用自定义过滤而不是 el-select 自带的 filterable：自带的那个是大小写敏感的字面
// 包含，输入小写 au 找不到「黄金 AU」，更别说用拼音首字母找「高盛」。
const memberQuery = ref('')
const filteredMembers = computed(() =>
  members.value.filter((name) => searchHit(name, memberQuery.value))
)

/**
 * 中文输入法正在预编辑时，也把当前输入当作查询。
 *
 * 不加这个，拼音搜索在中文输入法下**完全不工作**：el-select 的 handleQueryChange
 * 头一行就是 `if (states.previousQuery === val || isComposing.value) return`，
 * 预编辑期间根本不调 filter-method。
 * 那条短路是为「打中文」设计的——敲拼音时先别搜，等选完字再搜。可我们这个搜索框
 * 的主力用法恰恰是**永远不选字**：输入 gs 找「高盛期货」，要的就是这两个字母本身，
 * IME 一直停在预编辑态，composition 永不结束，查询就永远是空的
 * （2026-08-19 运营者报「输入 gs 完全出不来」，实测挂真实组件对拍：英文输入法
 * 下查询是 'gs'、下拉只剩高盛，中文输入法下查询恒为空串、列表原封不动）。
 *
 * composition 事件会冒泡到 el-select 根节点，所以在外层挂一次就够，
 * 不必去够组件内部那个 input。选完字后 el-select 自己会再走一遍 filter-method，
 * 两条路写的是同一个 memberQuery，不冲突。
 */
function onComposing(event: CompositionEvent) {
  const input = event.target as HTMLInputElement | null
  if (input && typeof input.value === 'string') memberQuery.value = input.value
}

// —— 收藏 ——
//
// 盯的常常是固定的那一组机构，每次进来重勾五家是纯粹的重复劳动。
const favorites = ref<SeatFavorite[]>([])
const favoriteName = ref('')
const savingFavorite = ref(false)

async function loadFavorites() {
  try {
    const { data } = await getSeatFavorites()
    favorites.value = data
  } catch {
    // 收藏读不到不影响看数据，页面照常用。
  }
}

/**
 * 取写入保护令牌。
 *
 * store 里这个令牌是**懒加载**的：刚进页面或刷新之后它是 null，要先 loadCsrf()。
 * 直接拿 `csrfToken ?? ''` 送出去，后端会以 403「request is not allowed」拒掉，
 * 而错误信息里看不出缺的是令牌——2026-08-15 就是这么坏的。
 */
async function csrf() {
  if (!auth.csrfToken) await auth.loadCsrf()
  if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
  return auth.csrfToken
}

async function saveFavorite() {
  const name = favoriteName.value.trim()
  if (!name) {
    ElMessage.warning('给这组席位起个名字')
    return
  }
  if (!selected.value.length) {
    ElMessage.warning('先选几个席位')
    return
  }
  savingFavorite.value = true
  try {
    const { data } = await createSeatFavorite({ name, members: selected.value }, await csrf())
    favorites.value = [data, ...favorites.value]
    favoriteName.value = ''
    ElMessage.success(`已收藏「${data.name}」`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '收藏失败')
  } finally {
    savingFavorite.value = false
  }
}

function applyFavorite(favorite: SeatFavorite) {
  // 整组替换，不是并集：他点的是「换成这一组」。并进去会悄悄超过十家上限，
  // 而且他分不清哪几家是刚点进来的。
  selected.value = [...favorite.members]
}

async function removeFavorite(favorite: SeatFavorite) {
  try {
    await ElMessageBox.confirm(`删掉收藏「${favorite.name}」？`, '确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
  } catch {
    return // 点了取消
  }
  try {
    await deleteSeatFavorite(favorite.id, await csrf())
    favorites.value = favorites.value.filter((item) => item.id !== favorite.id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

/** 席位持仓：每家一段。接口一次只认一家，勾了几家就并发取几份。 */
interface MemberRows {
  member: string
  rows: SeatPositionRow[]
  costs: SeatContractCost[]
}
const memberRows = ref<MemberRows[]>([])

async function loadPositions() {
  loadingPositions.value = true
  try {
    // 先取一次名录与可选日期。不带 member 时接口给的是全市场那天的行，我们只要
    // 它捎带的 members / available_dates / trade_date 三样元数据。
    const { data: meta } = await getSeatPositions({
      tradeDate: tradeDate.value || undefined
    })
    members.value = meta.members
    availableDates.value = meta.available_dates
    if (meta.trade_date) tradeDate.value = meta.trade_date

    // 记住的那几家可能已经不在名录里（机构改名、退市、数据源换写法）。剔掉不认识的。
    const known = new Set(meta.members)
    const gone = selected.value.filter((name) => !known.has(name))
    if (gone.length) {
      ElMessage.info(`「${gone.join('、')}」已不在名录，已从所选中移除`)
      selected.value = selected.value.filter((name) => known.has(name))
    }
    // **一家不剩时不再自动补上名录第一个。**多选框里清空是「我要重挑」，
    // 替他塞一家回去，他刚腾出来的框又满了；配上加载期间禁用输入，
    // 「空着且能搜」这个状态根本不存在，等于搜索框废掉（2026-08-19 运营者报）。
    // 空着就空着，下面各子页会说明「先选几个席位」。

    // 并发取各家的持仓。上限十家，十个请求对自用面板是可接受的代价，换来的是
    // 后端契约一行不动。
    const fetched = await Promise.all(
      selected.value.map(async (name) => {
        const { data } = await getSeatPositions({
          member: name,
          tradeDate: tradeDate.value || undefined
        })
        return { member: name, rows: data.rows, costs: data.costs ?? [] }
      })
    )
    memberRows.value = fetched
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '席位持仓读取失败')
    memberRows.value = []
  } finally {
    loadingPositions.value = false
  }
}

async function loadBuilding() {
  if (!selected.value.length || !buildingInstrument.value) {
    days.value = []
    memberSeries.value = []
    latestMembers.value = []
    latestMembersDate.value = null
    buildingContracts.value = []
    return
  }
  loadingBuilding.value = true
  try {
    const { data } = await getSeatNetPosition({
      instrument: buildingInstrument.value,
      members: selected.value,
      contract: buildingContract.value || undefined,
      // 顶上那句「两个子页共用这组选择」此前只对了一半:会员共用了,交易日没有。
      // watch 一直在日期变化时重新请求,只是请求里没带日期,于是每次取回同一份。
      tradeDate: tradeDate.value || undefined
    })
    multiplier.value = data.price_multiplier
    days.value = data.days
    memberSeries.value = data.member_series ?? []
    latestMembers.value = data.latest_members
    latestMembersDate.value = data.latest_trade_date
    buildingContracts.value = data.contracts
    priceSeriesKind.value = data.price_series_kind
    // 上次记住的合约可能已经到期了——期货合约会到期，这是常态不是异常。
    // contracts 是所选席位在这个品种上历史持有过的全部合约（取并集），不在里面
    // 就没有可看的东西，退回合约汇总。留在那里只会显示一张空表，看上去像数据坏了。
    if (buildingContract.value && !data.contracts.includes(buildingContract.value)) {
      buildingContract.value = '' // 触发 watch 重新取一次汇总档
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '净持仓读取失败')
    days.value = []
    memberSeries.value = []
    latestMembers.value = []
    latestMembersDate.value = null
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
  if (route.query.tab === 'pnl') tab.value = 'pnl'
  loadVarietyNames()
  loadFavorites()
  loadPositions()
})
// deep 是必须的：多选框改的是数组内容，浅比较看不出来。
// loadPositions 里会剔掉已不在名录的那几家，那次写回会再触发一轮——第二轮
// 没有可剔的了，到此为止，不会打转。
watch(
  [selected, tradeDate],
  () => {
    loadPositions()
    if (tab.value === 'building') loadBuilding()
    if (tab.value === 'pnl') loadPnl()
  },
  { deep: true }
)
watch(tab, (value) => {
  if (value === 'pnl') loadPnl()
  router.replace({ query: { ...route.query, tab: value } })
  if (value === 'building') loadBuilding()
})
watch([buildingInstrument, buildingContract], () => loadBuilding())

// 盈亏商品(DEC-157):区间逐日盯市。品种留空 = 按席位模式(共用选择器里勾中的
// 每家各一张卡);选了品种 = 该品种全部席位排行,此时勾选的席位只影响别的子页。
const pnlInstrument = ref('')
const pnlStart = ref('')
const pnlEnd = ref('')
const pnlInstruments = ref<string[]>([])
// 每张卡记住**自己**的模式:member 卡的行是品种代码(要翻中文),instrument 卡的
// 行是席位名(本来就是中文)。渲染若跟着当前选择框走,切换品种的瞬间旧 member 卡
// 会退化成裸代码 AU/FG——运营者 2026-08-30 抓的就是这个。
const pnlCards = ref<{ title: string; mode: 'member' | 'instrument'; items: PnlBreakdownItem[] }[]>([])
const loadingPnl = ref(false)

/** 默认区间:最近 5 个交易日。
 * availableDates 是**倒序**的(最新在前,历史表一律倒序的平台惯例)——上线首日
 * 按升序尾巴取,默认区间落在了 2008-01(黄金数据的第一天)而且起止倒挂,
 * 后端 invalid_date_range 直接拒。这里先排序再取,不赌接口的顺序。 */
function defaultPnlRange() {
  const dates = [...availableDates.value].sort()
  if (!dates.length) return
  if (!pnlEnd.value) pnlEnd.value = dates[dates.length - 1]
  if (!pnlStart.value) pnlStart.value = dates[Math.max(0, dates.length - 5)]
}

async function loadPnl() {
  defaultPnlRange()
  if (!pnlStart.value || !pnlEnd.value) return
  // 手选也可能把起止点反,后端会拒;前端直接掉个头,别把错误弹给人看。
  if (pnlStart.value > pnlEnd.value) {
    ;[pnlStart.value, pnlEnd.value] = [pnlEnd.value, pnlStart.value]
  }
  const range = { startDate: pnlStart.value, endDate: pnlEnd.value }
  loadingPnl.value = true
  try {
    if (pnlInstrument.value) {
      const res = await getSeatPnlBreakdown({ instrument: pnlInstrument.value, ...range })
      pnlInstruments.value = res.data.all_instruments
      pnlCards.value = [
        {
          title: `${varietyLabel(pnlInstrument.value)} · 全部席位`,
          mode: 'instrument',
          items: res.data.items,
        },
      ]
    } else {
      // 一家一张卡。接口一次只认一家,勾了几家就并发取几份(与席位持仓同款)。
      const members = selected.value
      if (!members.length) {
        pnlCards.value = []
        return
      }
      const results = await Promise.all(
        members.map((member) => getSeatPnlBreakdown({ member, ...range }))
      )
      if (results.length) pnlInstruments.value = results[0].data.all_instruments
      pnlCards.value = results.map((res, i) => ({
        title: members[i],
        mode: 'member' as const,
        items: res.data.items,
      }))
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '盈亏商品读取失败')
  } finally {
    loadingPnl.value = false
  }
}

/** 条形宽度:同卡内按绝对值最大的那根归一。 */
function pnlBarWidth(items: PnlBreakdownItem[], item: PnlBreakdownItem) {
  const max = Math.max(...items.map((x) => Math.abs(Number(x.pnl) || 0)), 1)
  return `${Math.max(2, (Math.abs(Number(item.pnl) || 0) / max) * 100).toFixed(1)}%`
}

function pnlText(value: string) {
  const n = Number(value) || 0
  const abs = Math.abs(n)
  const wan = abs >= 10000 ? `${(n / 10000).toFixed(abs >= 1000000 ? 0 : 1)} 万` : `${Math.round(n)}`
  return n >= 0 ? `+${wan}` : `-${wan.replace('-', '')}`
}

watch([pnlInstrument, pnlStart, pnlEnd], () => {
  if (tab.value === 'pnl') loadPnl()
})

/** 所选那几家当天持有的品种（并集），用于「筛选商品显示」。 */
const instruments = computed(() =>
  [...new Set(memberRows.value.flatMap((entry) => entry.rows.map((row) => row.instrument)))].sort()
)

/**
 * 净持仓的品种选项：所选席位**历史上持有过的全部品种**，取并集。
 *
 * 不能只列「所选日期当天在榜」的——净持仓是历史序列,不受所选交易日限制。
 * 高盛 2026-08-17 掉出金榜,黄金就从下拉里消失了,而他 691 天的持仓序列明明都在
 * (运营者当日报的 bug)。历史列表取不到时(接口瞬断)退回当天在榜 + 已选,
 * 宁可少列不可空白。
 *
 * 取并集而不是交集：某个品种只有其中一家持有过，只列交集会让它整个消失，而
 * 那一家在这个品种上的持仓是实实在在要看的。
 */
const memberInstruments = ref<string[]>([])
watch(
  selected,
  async (names) => {
    memberInstruments.value = []
    if (!names.length) return
    try {
      const lists = await Promise.all(
        names.map(async (name) => (await getSeatMemberInstruments(name)).data.instruments)
      )
      memberInstruments.value = [...new Set(lists.flat())]
    } catch {
      // 退回旧口径,不打断页面。
    }
  },
  { immediate: true, deep: true }
)
const buildingInstrumentOptions = computed(() => {
  const list = new Set([...memberInstruments.value, ...instruments.value])
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
  /** 该行含回榜反推成分:那天实际未上榜,数字由回榜日增减倒推。 */
  inferred: boolean
  /** 该合约的净持仓成本(推算),由后端按合约算好。null = 不知道,不是 0。 */
  cost: string | null
  costNet: string | null
  costReason: string | null
}
interface InstrumentBlock {
  instrument: string
  netTotal: number
  netChange: number | null | undefined
  contracts: ContractLine[]
}

// 增减量三态（未知 / 无行 / 真值）与吸收律见 src/seatChange.ts，那里有测试盯着。

function buildBlocks(rows: SeatPositionRow[], costs: SeatContractCost[] = []): InstrumentBlock[] {
  const wanted = new Set(instrumentFilter.value)
  const byInstrument = new Map<string, Map<string, ContractLine>>()
  const costByKey = new Map(costs.map((c) => [`${c.instrument}|${c.contract}`, c]))
  for (const row of rows) {
    if (row.is_variety_total || !row.contract) continue
    if (wanted.size && !wanted.has(row.instrument)) continue
    if (row.rank_type === 'volume') continue
    const contracts = byInstrument.get(row.instrument) ?? new Map()
    const hit = costByKey.get(`${row.instrument}|${row.contract}`)
    const line = contracts.get(row.contract) ?? {
      contract: row.contract,
      long: 0,
      longChange: undefined,
      short: 0,
      shortChange: undefined,
      inferred: false,
      cost: hit?.cost ?? null,
      costNet: hit?.net_position ?? null,
      costReason: hit?.cost_unknown_reason ?? null
    }
    // 任一腿来自回榜反推,整行标「推算」:那天他实际未上榜,数字是倒推的。
    if (row.source === 'reboard_inferred') line.inferred = true
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
}

/**
 * 席位持仓：每家一段，段首写家名。
 *
 * 不合并成一张表——榜单的「名次」与「增减量」是逐家公布的，加起来没有意义。
 * 要看合计去净持仓子页，那边本来就是干这个的。
 */
const memberBlocks = computed(() =>
  memberRows.value.map((entry) => ({
    member: entry.member,
    blocks: buildBlocks(entry.rows, entry.costs)
  }))
)

/** 成本列的数:最多两位小数,千分位。 */
function fmtCost(value: string) {
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
/** 成本对应的净持仓方向与手数——成本是「净持仓」的成本,不标清楚方向这个数没法用。 */
function costNetLabel(value: string) {
  const n = Number(value)
  if (n === 0) return '净持仓 0'
  return `${n > 0 ? '净多' : '净空'} ${fmt(Math.abs(n))} 手`
}
/** 成本为空的原因,说人话。引擎给的是枚举,不认识的原样显示,别吞掉。 */
function costReasonText(reason: string | null) {
  switch (reason) {
    case 'seat_off_the_board':
      return '那天不在前 20 榜上,持仓未知'
    case 'no_settlement_on_add':
      return '建仓当日无结算价,成本不可知'
    case 'no_record_that_day':
      return '那天该合约没有他的记录'
    case null:
      return '仓位为零'
    default:
      return reason
  }
}

function openBuilding(instrument: string, contract?: string) {
  buildingInstrument.value = instrument
  buildingContract.value = contract ?? ''
  tab.value = 'building'
}

const signed = signedChange
const fmt = (value: number) => value.toLocaleString('zh-CN')

// —— 净持仓的四联图 ——
const dates = computed(() => days.value.map((day) => day.trade_date))
// 掉榜且反推不出的日子按 0 画(2026-08-16 运营者拍板:折线不留缺口,回测
// 口径同引擎的「掉榜=不在场」)。**0 只进这条展示曲线**:掉榜底色标注、
// 小窗「按 0 计入」说明、成本与盈亏的三态口径(掉榜=未知)全部保持——
// 把 0 喂给成本链曾造出 16 万行假盈亏(DEC-048),别再来一次。
// 交易所未公布排名的日子(DEC-130)留空:那天整张榜不存在,画 0 会被读成清仓,
// 与「掉榜按 0 画」不是一回事 —— 掉榜是榜在人不在,日历上还有别家撑着曲线。
const netSeries = computed(() =>
  days.value.map((day) => (day.unpublished ? null : (num(day.net_position) ?? 0)))
)
const pnlSeries = computed(() => days.value.map((day) => num(day.daily_pnl)))
const cumulativeSeries = computed(() => days.value.map((day) => num(day.cumulative_pnl)))

// 国内看盘的惯例：红涨绿跌。盈亏柱按正负着色，一眼能看出哪天在赚。
// 颜色在构建时从 chartTokens() 取当前主题值，不能提到模块顶层缓存。
function pnlBars(values: Array<number | null>) {
  const tokens = chartTokens()
  return values.map((value) => ({
    value,
    itemStyle: { color: (value ?? 0) >= 0 ? tokens.up : tokens.down }
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
    Number(day.long_cost_lots) < Number(day.long_lots)
    || Number(day.short_cost_lots) < Number(day.short_lots)
  ).length
)

// —— 掉榜区间 ——
//
// 交易所只发前 20 名。某家掉出榜单的那些天，官方文件里没有他这一行——那是
// 「不知道」，不是「零」。合计里少了他，曲线上那道台阶看起来却像是减仓，
// 所以把这些天用底色圈出来并写明原因。
//
// 多选之后判据从「净持仓是 null」换成「有几家没算进来」：合计接口给的净持仓
// 永远是个数（在榜那几家的和），少算了谁记在 `missing_members` 里。只勾一家时
// 两者等价——那家掉榜，`missing_members` 就是他自己。
// 未公布日(DEC-130)不算掉榜:整张榜不存在,不是谁掉了。
const offBoardDays = computed(
  () => days.value.filter((day) => day.missing_members.length > 0 && !day.unpublished).length
)
const inferredDays = computed(
  () => days.value.filter((day) => day.inferred_members.length > 0).length
)

// 区间合并在 offBoard.ts 里，那边有测试：差一格就会把回榜那天也涂成空白。
const bands = computed(() =>
  offBoardBands(
    days.value.map((day) => ({
      trade_date: day.trade_date,
      // 未公布日不进掉榜带:它另有一条带、另一句话(DEC-130)。
      known: day.missing_members.length === 0 || Boolean(day.unpublished)
    }))
  )
)
/** 交易所未公布排名的天数(DEC-130):大商所只对持仓量 ≥2 万手的合约发排名。 */
const unpublishedDays = computed(() => days.value.filter((day) => day.unpublished).length)
// 未公布带:与掉榜带同一套机械,用中性灰而不是警示色 —— 这不是谁的问题,是榜不存在。
const unpublishedBands = computed(() =>
  offBoardBands(
    days.value.map((day) => ({
      trade_date: day.trade_date,
      known: !day.unpublished
    }))
  ).map(
    ([start, end]): Band => [
      { ...start, itemStyle: { color: withAlpha(chartTokens().axisLabel, 0.12) } },
      end
    ]
  )
)

/** token 没有单独的图表警示底色，由 accent 色值加透明度得出。 */
function withAlpha(hex: string, alpha: number) {
  const value = parseInt(hex.slice(1), 16)
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`
}

// 推算日的底色带:与掉榜带同一套机械,颜色更淡以示「有数,但数是倒推的」。
// itemStyle 挂在段首元素上——ECharts markArea 按段首取样式。
const inferredBands = computed(() =>
  offBoardBands(
    days.value.map((day) => ({
      trade_date: day.trade_date,
      known: day.inferred_members.length === 0
    }))
  ).map(
    ([start, end]): Band => [
      { ...start, itemStyle: { color: withAlpha(chartTokens().accent, 0.07) } },
      end
    ]
  )
)

const offBoardMark = computed(() => ({
  silent: true,
  itemStyle: { color: withAlpha(chartTokens().accent, 0.16) },
  label: { show: false },
  data: [...bands.value, ...inferredBands.value, ...unpublishedBands.value]
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
  const tokens = chartTokens()
  const parts = [`<div style="margin-bottom:4px"><b>${day.trade_date}</b></div>`, ...head]

  // 合约按净方向分两组：净多的那几个是「多单」，净空的是「空单」，两者相减才是
  // 净持仓——这是运营者定的口径。选了单合约时同一套照跑：那时只有一条腿有手数。
  // 手数紧跟自己那条腿的成本，不要两个手数并排再两个成本并排（那样眼睛得在四个
  // 数之间来回配对，运营者 2026-08-18 指出过）。
  const long = Number(day.long_lots)
  const short = Number(day.short_lots)
  if (long > 0) {
    parts.push(row('多单', lots(long), tokens.up))
    parts.push(row('　净持仓成本（推算）', legCost(day.long_cost, day.long_cost_lots, day.long_lots)))
  }
  if (short > 0) {
    parts.push(row('空单', lots(short), tokens.down))
    parts.push(
      row('　净持仓成本（推算）', legCost(day.short_cost, day.short_cost_lots, day.short_lots))
    )
  }

  const net = Number(day.net_position)
  parts.push(
    row(
      '合计净持仓',
      lots(Math.abs(net)) + (net === 0 ? '' : net > 0 ? '（净多）' : '（净空）'),
      net === 0 ? undefined : net > 0 ? tokens.up : tokens.down
    )
  )
  parts.push(row('计入席位', `${day.counted_members.length} 家`))
  // 逐家两条腿(DEC-132,运营者 2026-08-24):悬停到哪天就看哪天各家的多空单与成本推算,
  // 与顶上「最新一天」那两行同一套口径。掉榜/未公布的那家写明,不display 0。
  if (!day.unpublished) {
    for (const ms of memberSeries.value) {
      const leg = ms.legs[index] ?? null
      if (leg === null) {
        parts.push(row(`　${ms.member}`, `<span style="color:${tokens.accent}">掉榜，持仓未知</span>`))
        continue
      }
      const l = Number(leg.l)
      const sLots = Number(leg.s)
      const bits: string[] = []
      if (l > 0) {
        bits.push(`<span style="color:${tokens.up};font-weight:600">多 ${lots(l)}</span>`
          + (leg.lc === null ? '' : ` 均价 ${price(Number(leg.lc))}`))
      }
      if (sLots > 0) {
        bits.push(`<span style="color:${tokens.down};font-weight:600">空 ${lots(sLots)}</span>`
          + (leg.sc === null ? '' : ` 均价 ${price(Number(leg.sc))}`))
      }
      if (!bits.length) bits.push('0 手')
      parts.push(row(`　${ms.member}`, bits.join(' · ')))
    }
  }
  // 掉榜与反推都要逐个点名。只说「少了一家」，看的人不知道少的是不是他最在意的那家。
  if (day.inferred_members.length) {
    parts.push(
      row(
        '按反推计入',
        `<span style="color:${tokens.accent}">${day.inferred_members.join('、')}（实际未上榜，数字由回榜日倒推）</span>`
      )
    )
  }
  if (day.unpublished) {
    parts.push(row('持仓排名', '交易所未公布（持仓量 <2 万手不发排名）· 持仓未知，不是掉榜'))
  } else if (day.missing_members.length) {
    parts.push(
      row(
        '当日掉榜',
        `<span style="color:${tokens.accent}">${day.missing_members.join('、')}（未计入）</span>`
      )
    )
  }

  // 「估计」二字不能省：这是由公开持仓与结算价推出来的，不是他的对账单。
  // 这里只放累计：当日那个数由「当日盈亏」图自己的小窗放在第一行（dailyPnlTooltip），
  // 免得在当日盈亏的柱子上悬停却只读到累计（2026-08-18 运营者指出的口径错配）。
  const cumulative = num(day.cumulative_pnl)
  if (cumulative !== null) {
    parts.push(row('估计累计盈利',
      `${cumulative >= 0 ? '+' : '−'}${money(Math.abs(cumulative))}`,
      cumulative >= 0 ? tokens.up : tokens.down))
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
  // **跟后端定的 as-of 日走,不取序列最后一天。**
  // 交易日选 8.19 时这行要显示 8.19 的各家情况,而**图保持完整历史不截断**——
  // 运营者 2026-08-21 的原话:「应该只改净持仓的多单空单显示,就是改上面的文字,
  // 方便我看各家情况,其他全部不用变」。哪天算 as-of 由后端 `as_of_day` 一处决定,
  // 摘要与下面那排各家分腿都读它,两半不会各判各的。
  // **不给兜底**:后端没给 as-of 日,说明选中日早于全部数据,那天他确实没有持仓
  // 可看。这时退回显示最新一天等于默默无视选择,而那正是这次要修的毛病。
  const day = days.value.find((d) => d.trade_date === latestMembersDate.value)
  if (!day) return null
  const net = Number(day.net_position)
  const parts: Array<{ text: string; tone?: 'up' | 'down' | 'warn' }> = []
  const long = Number(day.long_lots)
  const short = Number(day.short_lots)
  // 手数紧跟自己那条腿的成本。净持仓是个**差**，单看它说不出持仓结构：
  // 净 2,415 可能是「多 2,436 空 21」，也可能是「多 5,000 空 2,585」。
  if (long > 0) {
    parts.push({ text: `多单 ${lots(long)}`, tone: 'up' })
    parts.push({ text: `均价 ${legCost(day.long_cost, day.long_cost_lots, day.long_lots)}` })
  }
  if (short > 0) {
    parts.push({ text: `空单 ${lots(short)}`, tone: 'down' })
    parts.push({ text: `均价 ${legCost(day.short_cost, day.short_cost_lots, day.short_lots)}` })
  }
  parts.push({
    text: `净持仓 ${lots(Math.abs(net))}${net === 0 ? '' : net > 0 ? '（净多）' : '（净空）'}`,
    tone: net === 0 ? undefined : net > 0 ? 'up' : 'down'
  })
  parts.push({ text: `计入 ${day.counted_members.length} 家` })
  if (day.inferred_members.length) {
    parts.push({ text: `${day.inferred_members.join('、')} 未上榜，按回榜反推计入`, tone: 'warn' })
  }
  if (day.unpublished) {
    parts.push({ text: '交易所当日未公布该合约持仓排名（持仓量 <2 万手），持仓未知', tone: 'warn' })
  } else if (day.missing_members.length) {
    parts.push({ text: `${day.missing_members.join('、')} 当日掉榜，未计入`, tone: 'warn' })
  }
  return { date: day.trade_date, parts }
})

/**
 * 摘要下面那一排：最新一天**逐家**的多空手数与均价。
 *
 * 合计那排说不出持仓结构里「谁在多、谁在空」——五家合起来净空一万四千手，
 * 可能是五家都在空，也可能一家重仓空、四家轻仓多，两种情形该有的判断完全不同
 * （运营者 2026-08-19 要求拆开看）。
 *
 * 手数与均价的三态照旧：掉榜是**未知**不是零，成本覆盖不全要说覆盖了多少手。
 * 数据由后端按 member 分组各跑一遍同一套成本引擎算出，与合计同源。
 */
const latestLegs = computed(() =>
  latestMembers.value.map((leg) => {
    const long = Number(leg.long_lots)
    const short = Number(leg.short_lots)
    return {
      member: leg.member,
      missing: leg.missing,
      inferred: leg.inferred,
      long: long > 0 ? { lots: lots(long), cost: legCost(leg.long_cost, leg.long_cost_lots, leg.long_lots) } : null,
      short:
        short > 0
          ? { lots: lots(short), cost: legCost(leg.short_cost, leg.short_cost_lots, leg.short_lots) }
          : null,
      // 在榜但多空相等：他今天有行，只是净头寸为零。与掉榜是两回事。
      flat: !leg.missing && long === 0 && short === 0
    }
  })
)

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

/**
 * 「当日盈亏」图专用的小窗:**第一行必须是当日那个数**。
 *
 * 三张图共用同一个小窗内容时,悬停在当日盈亏的柱子上读到的却是「估计累计盈利」
 * ——图名与数字对不上,运营者 2026-08-18 就是这么发现的。共用小窗省事,但代价是
 * 用户在哪张图上,就应该先看到那张图画的东西。
 */
const dailyPnlTooltip = {
  trigger: 'axis' as const,
  formatter: (params: unknown) => {
    const index = axisIndex(params)
    if (index === null) return ''
    const day = days.value[index]
    const tokens = chartTokens()
    const value = day ? num(day.daily_pnl) : null
    const head =
      value === null
        ? [row('当日盈亏', '不可知（掉出前 20 或当日无结算价）')]
        : [
            row(
              '当日盈亏',
              `${value >= 0 ? '+' : '−'}${money(Math.abs(value))}`,
              value >= 0 ? tokens.up : tokens.down
            )
          ]
    return tooltipBody(index, head)
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
    ...sliderStyle(),
    labelFormatter: (value: number) => dates.value[Math.round(value)] ?? ''
  }
])
/** 留给滑钮的高度。忘了加就是滑钮压在横轴标签上。 */
const GRID_BOTTOM = 62

const priceOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 60, right: 24, top: 24, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    // K 线图上原来还有一条成本蓝线，运营者要求撤掉：图上只留行情，成本进小窗。
    // 数一个没少，见 tooltipBody。
    tooltip: {
      ...tooltip,
      ...tooltipStyle(),
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
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    yAxis: {
      type: 'value' as const,
      scale: true,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: [
      {
        name: 'K线',
        type: 'candlestick' as const,
        data: candles.value as unknown as CandlestickSeriesOption['data'],
        // 红涨绿跌，国内惯例：阳线 = up（红），阴线 = down（绿）。
        itemStyle: {
          color: tokens.up,
          color0: tokens.down,
          borderColor: tokens.up,
          borderColor0: tokens.down
        },
        // 掉榜区间的底色原先挂在成本线上，成本线撤了就得挪过来，否则整段标注消失。
        markArea: offBoardMark.value
      }
    ]
  }
})
const netOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    tooltip: { ...tooltip, ...tooltipStyle() },
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    // scale 不能开：净持仓要看得出离零轴多远，多空翻向也全靠零轴分界。
    yAxis: {
      type: 'value' as const,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
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
          lineStyle: { color: tokens.baseline, type: 'dashed' as const },
          label: { show: false }
        }
      }
    ]
  }
})
const pnlOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    tooltip: { ...dailyPnlTooltip, ...tooltipStyle() },
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { formatter: money, color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: [{ name: '当日盈亏', type: 'bar' as const, data: pnlBars(pnlSeries.value) }]
  }
})
const cumulativeOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    tooltip: { ...tooltip, ...tooltipStyle() },
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { formatter: money, color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: [
      { name: '合约累计盈亏', type: 'bar' as const, data: pnlBars(cumulativeSeries.value) }
    ]
  }
})
/** 末日累计值，放在标题旁边——图能看趋势，数字才好念。 */
const cumulativeTotal = computed(() => {
  const last = cumulativeSeries.value.filter((value) => value !== null).pop()
  return last === undefined || last === null ? null : last
})

/**
 * 最新一天的当日盈亏，常驻在「当日盈亏」标题旁——与累计那张图同一个待遇。
 *
 * 取的是**序列最后一天**，不是「最后一个有值的天」：累计线可以跳过不可知的天
 * 接着算（那条线本来就是已知部分的累加），但「今天赚了多少」跳过去就成了
 * 拿前几天的数冒充今天。最后一天不可知就如实说不可知。
 */
const latestDailyPnl = computed(() => {
  if (!days.value.length) return null
  const value = num(days.value[days.value.length - 1].daily_pnl)
  return { known: value !== null, value: value ?? 0 }
})

</script>

<template>
  <section class="seats">
    <header class="page-head">
      <h1>席位</h1>
      <p>选几个会员和一个交易日，两个子页共用这组选择。</p>
    </header>

    <el-card shadow="never" class="shared">
      <div class="control-row">
        <!-- 不折叠：勾着的是哪几家，一眼要看全。折成「国泰君安 +4」等于把选择
             藏进一个悬浮层，而这组选择正是两个子页共用的那一组，最该常驻可见。
             标签多了就自动换行增高，这个框允许长高。 -->
        <el-select
          v-model="selected"
          multiple
          filterable
          :filter-method="(value: string) => (memberQuery = value)"
          :multiple-limit="MAX_MEMBERS"
          class="member-select"
          :placeholder="`选择席位（最多 ${MAX_MEMBERS} 家）`"
          :disabled="loadingPositions && !members.length"
          @compositionupdate="onComposing"
        >
          <el-option v-for="name in filteredMembers" :key="name" :label="name" :value="name" />
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
      <!-- 收藏的是「这一组席位」。盯的常常是固定的那几家，每次进来重勾五家是
           纯粹的重复劳动。点收藏是**整组替换**，不是并进去。 -->
      <div class="favorites">
        <el-input
          v-model="favoriteName"
          placeholder="给这组席位起个名字"
          style="width: 200px"
          :disabled="savingFavorite"
          @keyup.enter="saveFavorite"
        />
        <el-button :loading="savingFavorite" @click="saveFavorite">收藏当前这组</el-button>
        <el-tag
          v-for="favorite in favorites"
          :key="favorite.id"
          closable
          class="favorite-tag"
          @click="applyFavorite(favorite)"
          @close="removeFavorite(favorite)"
        >
          {{ favorite.name }}（{{ favorite.members.length }} 家）
        </el-tag>
      </div>

      <div class="tabs">
        <el-radio-group v-model="tab">
          <el-radio-button value="positions">席位持仓</el-radio-button>
          <el-radio-button value="building">净持仓</el-radio-button>
          <el-radio-button value="pnl">盈亏商品</el-radio-button>
        </el-radio-group>
      </div>
    </el-card>

    <!-- 一家没勾时两个子页都没东西可画。**空着是允许的状态**（清空是为了重挑，
         不该被自动塞回一家），所以要有人出来说一句，而不是留一片空白让人以为坏了。 -->
    <el-card v-if="tab !== 'pnl' && !selected.length" shadow="never">
      <el-empty description="先在上面选几个席位" />
    </el-card>

    <!-- 盈亏商品(DEC-157):区间逐日盯市。不选品种 = 勾中的每家席位各一张
         逐品种卡;选了品种 = 该品种全部席位排行(此时勾选不影响本页)。 -->
    <template v-if="tab === 'pnl'">
      <el-card shadow="never" class="filter-card">
        <div class="pnl-controls">
          <el-select
            v-model="pnlInstrument"
            clearable
            placeholder="按席位看(或选一个品种)"
            style="width: 220px"
          >
            <el-option
              v-for="code in pnlInstruments"
              :key="code"
              :label="varietyLabel(code)"
              :value="code"
            />
          </el-select>
          <el-date-picker
            v-model="pnlStart"
            type="date"
            style="width: 150px"
            placeholder="起始日"
            value-format="YYYY-MM-DD"
            :clearable="false"
            :disabled-date="isNotTradingDay"
          />
          <span class="pnl-range-dash">至</span>
          <el-date-picker
            v-model="pnlEnd"
            type="date"
            style="width: 150px"
            placeholder="截止日"
            value-format="YYYY-MM-DD"
            :clearable="false"
            :disabled-date="isNotTradingDay"
          />
        </div>
      </el-card>
      <el-card v-if="!pnlInstrument && !selected.length" shadow="never">
        <el-empty description="先在上面选几个席位,或在左上选一个品种" />
      </el-card>
      <el-card
        v-for="card in pnlCards"
        v-else
        :key="card.title"
        shadow="never"
        v-loading="loadingPnl"
      >
        <template #header>
          <span class="pnl-card-title">{{ card.title }} 盈亏分布(估)</span>
          <span class="pnl-card-sub">{{ pnlStart }} 至 {{ pnlEnd }} · 逐日盯市 · 掉榜沿用上次持仓</span>
        </template>
        <el-empty v-if="!card.items.length" description="区间内没有可计算的持仓" />
        <div v-else class="pnl-rows">
          <div v-for="item in card.items" :key="item.key" class="pnl-row">
            <span class="pnl-key">{{ card.mode === 'instrument' ? item.key : varietyLabel(item.key) }}</span>
            <div class="pnl-bar-track">
              <div
                class="pnl-bar"
                :class="Number(item.pnl) >= 0 ? 'win' : 'loss'"
                :style="{ width: pnlBarWidth(card.items, item) }"
              />
            </div>
            <span class="pnl-value" :class="Number(item.pnl) >= 0 ? 'red' : 'green'">
              {{ item.no_multiplier ? '未配点值' : pnlText(item.pnl) }}
            </span>
            <span class="pnl-days">
              <template v-if="!item.no_multiplier">
                {{ item.known_days }} 天<template v-if="item.filled_days"
                  > · 含推算 {{ item.filled_days }} 天</template
                >
              </template>
            </span>
          </div>
        </div>
      </el-card>
    </template>

    <!-- 席位持仓按家分段：榜单的「名次」与「增减量」是逐家公布的，加起来没有
         意义，所以勾了几家就列几段，不合并。要看合计去净持仓子页。 -->
    <template v-else-if="tab === 'positions'">
      <!-- 筛选器只出一次：它是对所有分段生效的一个条件，每段各放一个会让人
           以为可以逐家分别筛。 -->
      <el-card shadow="never" class="filter-card">
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
      </el-card>
      <el-card v-for="entry in memberBlocks" :key="entry.member" shadow="never">
        <template #header>
          <div class="panel-head">
            <h2>{{ tradeDate }} {{ entry.member }} 席位持仓</h2>
          </div>
        </template>
        <el-empty v-if="!entry.blocks.length" description="这一天该席位没有持仓" />
        <table v-else class="positions">
          <thead>
            <tr>
              <th>品种</th>
              <th>总净持仓</th>
              <th>合约</th>
              <th>多头持仓</th>
              <th>空头持仓</th>
              <th title="由公开持仓变化与结算价推出,不是成交均价;净持仓计价、多空不分开,与净持仓页同一个引擎">
                成本<span class="th-sub">净持仓·推算</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-for="block in entry.blocks" :key="block.instrument">
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
                    品种汇总净持仓
                  </el-button>
                </td>
                <td class="contract">
                  <div>
                    {{ line.contract }}
                    <span
                      v-if="line.inferred"
                      class="inferred-tag"
                      title="该日实际未上榜(前 20 没有他),持仓由回榜日的增减倒推。"
                    >推算·未上榜</span>
                  </div>
                  <el-button
                    size="small"
                    @click="openBuilding(block.instrument, line.contract)"
                  >
                    净持仓
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
                <td class="figure cost">
                  <template v-if="line.cost !== null">
                    <div>{{ fmtCost(line.cost) }}</div>
                    <div v-if="line.costNet !== null" class="change">{{ costNetLabel(line.costNet) }}</div>
                  </template>
                  <div v-else class="change" :title="costReasonText(line.costReason)">
                    —<span class="why">{{ costReasonText(line.costReason) }}</span>
                  </div>
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
        <p v-if="selected.length > 1" class="note">
          这几家是<strong>加总</strong>看的：{{ selected.join('、') }} 共
          {{ selected.length }} 家，逐家逐合约各自算完再相加。想单看某一家，上面只勾他一个。
        </p>
        <p v-if="!buildingContract" class="note">
          合约汇总把所选席位在这个品种<strong>各个合约上的持仓逐一算完再相加</strong>：净多的那些
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
            <strong>{{ offBoardDays }}</strong> 天至少有一家掉出交易所前 20 榜，
            <strong>持仓未知</strong>——不是清仓。交易所只公布前 20 名，那些天文件里没有他这一行，
            合计里也就少算了他。图上以此底色标出；成本与累计盈亏在这几天原地保留，回榜后接着算。
          </span>
        </p>
        <p v-if="unpublishedDays" class="note muted">
          另有 <strong>{{ unpublishedDays }}</strong> 天<strong>交易所没有公布这个合约的持仓排名</strong>
          ——大商所只对持仓量 ≥ 2 万手的合约发排名，合约临近到期跌破 2 万手后就停发（生猪 LH2607
          6/24 起如此）。这不是席位掉榜，是整张榜不存在：K 线照画、持仓留空、底色为灰。
        </p>
        <p v-if="inferredDays" class="note muted">
          另有 <strong>{{ inferredDays }}</strong> 天至少有一家实际没上榜，持仓由回榜日的增减
          倒推得出，已计入合计（图上底色更淡的那几段）。
        </p>
      </el-card>

      <el-empty v-if="!loadingBuilding && !days.length" description="选一个品种，或所选席位在此品种上没有持仓" />
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
          <SpreadChart
            v-if="hasCandles"
            :option="priceOption"
            :height="320"
            group="seats-net-position"
            export-name="净持仓-行情"
          />
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
            <!-- 合计那排的下一排：拆到每一家。合计说不出「谁在多、谁在空」，
                 而那正是勾了好几家之后最要紧的一件事。 -->
            <div v-if="latestLegs.length" class="member-legs">
              <span v-for="leg in latestLegs" :key="leg.member" class="member-leg">
                <b>{{ leg.member }}</b>
                <template v-if="leg.missing">
                  <span class="warn">当日掉榜，持仓未知</span>
                </template>
                <template v-else>
                  <span v-if="leg.long" class="up">多 {{ leg.long.lots }}</span>
                  <span v-if="leg.long">均价 {{ leg.long.cost }}</span>
                  <span v-if="leg.short" class="down">空 {{ leg.short.lots }}</span>
                  <span v-if="leg.short">均价 {{ leg.short.cost }}</span>
                  <span v-if="leg.flat">当日无持仓</span>
                  <span v-if="leg.inferred" class="warn">推算</span>
                </template>
              </span>
            </div>
          </template>
          <SpreadChart
            :option="netOption"
            :height="300"
            group="seats-net-position"
            export-name="净持仓-合计"
          />
        </el-card>
        <el-card shadow="never">
          <template #header>
            <h2>
              当日盈亏
              <span
                v-if="latestDailyPnl"
                :class="
                  latestDailyPnl.known ? (latestDailyPnl.value >= 0 ? 'up' : 'down') : 'muted'
                "
              >
                <template v-if="!latestDailyPnl.known">当日不可知</template>
                <template v-else>
                  {{ latestDailyPnl.value >= 0 ? '当日盈利' : '当日亏损' }}
                  {{ money(Math.abs(latestDailyPnl.value)) }}
                </template>
              </span>
            </h2>
          </template>
          <SpreadChart
            :option="pnlOption"
            :height="300"
            group="seats-net-position"
            export-name="净持仓-当日盈亏"
          />
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
          <SpreadChart
            :option="cumulativeOption"
            :height="300"
            group="seats-net-position"
            export-name="净持仓-累计盈亏"
          />
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
  background: var(--tv-warn-bg);
  border: 1px solid color-mix(in srgb, var(--tv-warn) 50%, transparent);
}

.up {
  color: var(--tv-up);
  font-weight: 600;
  margin-left: 8px;
}
.down {
  color: var(--tv-down);
  font-weight: 600;
  margin-left: 8px;
}
/* 标题旁的「当日不可知」：与 .up/.down 同一位置，但不是盈亏，走弱化色。 */
h2 .muted {
  color: var(--tv-text-muted);
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
  display: flex;
  align-items: center;
  gap: 10px;
}
/* 席位框占满剩下的宽度，标签多了往下长而不是挤成一行。
   min-width 保证窄屏下它先换行而不是被压成一条缝。 */
.member-select {
  flex: 1 1 420px;
  min-width: 280px;
}
.favorites {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 收藏是拿来点的，光标要说明这件事——标签默认不像可点。 */
.favorite-tag {
  cursor: pointer;
}
/* 筛选器自成一行，与下面各家的分段卡片拉开一点。 */
.filter-card {
  margin-bottom: 12px;
}
/* 推算日的说明比掉榜弱一档：掉榜是「少算了」，推算是「算了，但数是倒推的」。 */
.note.muted {
  color: var(--tv-text-muted);
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
  color: var(--tv-text-muted);
}

/* 最新一天的摘要，挨着「净持仓」标题。窄屏换行而不是挤成一团。 */
.latest {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--tv-text-secondary);
}
.latest-date {
  font-weight: 600;
  color: var(--tv-text);
}
.latest .up {
  color: var(--tv-up);
  font-weight: 600;
  margin-left: 0;
}
.latest .warn {
  color: var(--tv-warn);
}
.latest .down {
  color: var(--tv-down);
  font-weight: 600;
  margin-left: 0;
}

/* 逐家那一排，在合计摘要下面。整排比合计淡一档——合计是主角，这排是它的拆解。

   用 grid 而不是 flex-wrap：**换行后各家要在列上对齐**。先前用 flex + 竖线分隔，
   分隔线是靠 `.member-leg + .member-leg` 的左内边距撑出来的，换到第二行的那家
   （永安期货）在行首却照样带着这段内边距，比上一行行首（国泰君安）缩进一截
   （运营者 2026-08-19 指出）。改成等宽列之后，第 n 家与第 n+列数 家天然同列，
   竖线也就不需要了——列间距本身就是分隔。 */
.member-legs {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 4px 24px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--tv-text-muted);
}
.member-leg {
  display: flex;
  align-items: baseline;
  gap: 6px;
  white-space: nowrap;
}
.member-leg b {
  color: var(--tv-text-secondary);
  font-weight: 600;
}
.member-leg .up {
  color: var(--tv-up);
  font-weight: 600;
}
.member-leg .down {
  color: var(--tv-down);
  font-weight: 600;
}
.member-leg .warn {
  color: var(--tv-warn);
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
.positions .th-sub {
  display: block;
  font-weight: 400;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.positions .cost .why {
  display: block;
  font-size: 11px;
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
.inferred-tag {
  display: inline-block;
  margin-left: 4px;
  padding: 0 4px;
  border: 1px solid var(--tv-warn);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-warn-bg);
  color: var(--tv-warn);
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  cursor: help;
}
.pnl-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.pnl-range-dash {
  color: var(--el-text-color-secondary);
}
.pnl-card-title {
  font-weight: 600;
}
.pnl-card-sub {
  margin-left: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pnl-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pnl-row {
  display: grid;
  grid-template-columns: 110px 1fr 110px 150px;
  align-items: center;
  gap: 10px;
}
.pnl-key {
  font-size: 13px;
  text-align: right;
}
.pnl-bar-track {
  height: 14px;
  background: var(--el-fill-color-lighter);
  border-radius: 3px;
  overflow: hidden;
}
.pnl-bar {
  height: 100%;
  border-radius: 3px;
}
.pnl-bar.win {
  background: var(--el-color-danger);
}
.pnl-bar.loss {
  background: var(--el-color-success);
}
.pnl-value {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  text-align: right;
}
.pnl-value.red {
  color: var(--el-color-danger);
}
.pnl-value.green {
  color: var(--el-color-success);
}
.pnl-days {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
