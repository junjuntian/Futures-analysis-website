<script setup lang="ts">
/**
 * 生猪机构资金:读引擎产出的 hog_signals.json,只渲染不计算。
 *
 * 与金银**分成两个组件**不是重复:两套信号形态根本不同——金银是逐家席位权重 ×
 * 多席位共振,生猪是八家合计流向(研究阶段实测:生猪逐家事件整体胜率只有 50%,
 * 而合计流向控制动量后 t=5.4~7.5)。硬塞进同一套模板只会让两边都别扭。
 *
 * 界面上有一件事是硬要求:**做多信号必须标注「未经验证」**。回测里多头 15 笔
 * 累计仅 +4.5%,而样本期内机构合计净持仓一天都没转成净多——它符合运营者
 * 「等机构转多就转向」的策略意图,但没有数据背书,不能让它看起来和空头一样可信。
 */
import { computed, onMounted, ref } from 'vue'
import { entryGateText } from '../entry-gate'
import { failureHint } from '../fetch-hint'
import { rollPressureHint, type RollPressureState } from '../roll-pressure-hint'
import { getSeatNetPosition, type MemberLeg as SeatCost,
  type FlowCode
} from '../api'

/**
 * 一个品种一份 JSON,由引擎按品种各写各的(失败也各自隔离)。
 * 组件只负责渲染,规则差异全在 payload 里——**不要在这里按品种写 if**:
 * 生猪只做空、玻璃纯碱双向,这类差异是引擎实测定的,前端硬编码一份迟早对不上。
 */
const props = defineProps<{ instrument: FlowCode }>()
const FILES: Record<FlowCode, string> = {
  LH: 'hog_signals.json', FG: 'fg_signals.json', SA: 'sa_signals.json',
  JD: 'jd_signals.json', JM: 'jm_signals.json'
}

interface MemberLeg {
  member: string
  net: number
  change: number | null
  /** 逐腿变化(DEC-149,五窗专属;组内各家卡不给):多单/空单各自较昨日的增减。
   *  掉榜日该腿不可知为 null。老 JSON 没有这两个键。 */
  change_long?: number | null
  change_short?: number | null
  on_board: boolean
}

/** 五窗括号文案(DEC-149,2026-08-26 二改):**单数字、随席位方向表述**——
 *  净多席位「净多±X」、净空席位「净空±X」(净空+11,988 = 空头仓位增了 11,988)。
 *  一改的两腿版(多+X 空+Y)运营者看过效果图后否了:「显示净多+4790就行了,
 *  不要两腿显示」。change_long/change_short 留在 payload 里不显示,别删引擎字段。
 *  歧义的根源不变:纯净数 "(−1万)" 会把净空席位的加空读成在减,方向词补上就清了。 */
function panelChg(m: MemberLeg): string | null {
  if (m.change === null || m.change === undefined || m.change === 0) return null
  const mag = fmt(Math.abs(m.change))
  if (m.net > 0) return `净多${m.change > 0 ? '+' : '-'}${mag}`
  if (m.net < 0) return `净空${m.change < 0 ? '+' : '-'}${mag}`
  return `净${m.change > 0 ? '+' : ''}${fmt(m.change)}`
}
interface HogTrade {
  side: 'short' | 'long'
  entry_date: string
  exit_date: string | null
  entry_px: number | null
  exit_px: number | null
  contract: string
  ret_pct: number
  hold_days: number
  exit_reason: string | null
  /** 换月接力(DEC-147):本笔是上一合约交割纪律出场后接回来的,值=来源合约。老 JSON 没有。 */
  rolled_from?: string | null
  /** campaign(DEC-133)附加:批次成本与持仓中的阵营状态。旧产物没有。 */
  batch_cost?: number | null
  /** 跟批加仓(DEC-135):本笔战役的单位数与各批成交明细;entry_px 为均价。 */
  units?: number
  entries?: Array<{ date: string; px: number }>
  /** 散户接盘确认(DEC-138,只展示当仓位分级):进场时散户是否在向对面加仓。 */
  retail_confirm?: boolean
  retail_now?: { net: number | null; chg5: number | null; opposite_adding: boolean } | null
  camp_net?: number | null
  camp_peak?: number | null
  unload_pct?: number | null
}
interface HogPayload {
  name: string
  unit: string
  data_date: string
  computed_at: string
  state: string
  contract: string
  price: number | null
  signal: {
    z: number | null
    enter: number
    net: number | null
    change: number | null
    win: number
    suggested_position: number | null
    /**
     * **今天这个信号往哪边进** —— 引擎算好的,前端不许自己推。
     *
     * 运营者 2026-08-21:「触发信号要显示做多或者做空,一触发就显示」。
     * 判据在引擎的 `entry_side`,`replay` 与 payload 共用同一份 ——
     * DEC-104 正是前端自己推进场判据推错的:页面写着「需达 1(现 2.09)」
     * 却又显示无持仓,因为它比的是机构那个数而引擎比的是散户那个。
     */
    entry_side: 'long' | 'short' | null
    /** 进不了场时卡在哪一条(强度未到 / 做多已关 / 要求回撤 / 背离 / 临近交割)。 */
    entry_blocked: string | null
  }
  position: HogTrade | null
  /**
   * 机构方向本身,与「要不要进场」分开报。做多支路虽然关着,运营者盯的正是
   * 「机构什么时候真的转成净多」这个拐点,得让他第一时间看见。
   */
  institution: {
    net: number | null
    side: 'net_long' | 'net_short' | null
    just_flipped_long: boolean
    long_enabled: boolean
    long_signal_now: boolean
    /**
     * 机构相对**本轮峰值**卸掉了多少 —— **只作展示,不进任何进出场判据**。
     *
     * 运营者 2026-08-21 提出「和机构反向要等机构出货出得差不多」。这个数此前
     * 页面上根本没有,他只能盯着净持仓曲线目测,所以先把它摆出来。
     *
     * **为什么只摆不用**(`research/REPORT_SA_UNLOAD_DEEP_v1.md`):作为进场判据
     * 它只在**纯碱、5 日窗口**上通过了全部检验(掉榜控制 / 逐年 5 之 6 / 非重叠 /
     * 置换第 0.2 百分位);玻璃样本外符号翻转、焦煤明确否、生猪鸡蛋数据不够验。
     * 横截面上没有支持,而且现行引擎持仓中位数在 20 日以上,那个 5 日效应用不上。
     *
     * `legs_now !== legs_at_peak` 时这个降幅**分不清是出货还是掉榜** ——
     * 五家掉两家会让合计净持仓下降而人家一手没动。实测这个混淆专门吃掉长窗口
     * (纯碱 20 日的表观效应几乎全由它贡献),所以界面必须把它说出来。
     */
    unload: {
      /** 0~1;掉榜、刚建仓或刚换组时为 null。 */
      pct: number | null
      peak_net: number | null
      peak_date: string | null
      legs_now: number | null
      legs_at_peak: number | null
    }
  }
  /**
   * 散户反向维度(DEC-085)。这三家在多个品种上长期站多头、长期亏钱,所以反向取用。
   *
   * **现行策略(方案 C,DEC-086)就是用它进出场**:与机构共振时按它的方向进场。
   * 也就是说「进场条件」比的是 `retail.z`,**不是** `signal.z`。
   *
   * 这里原先写的是「当前只作展示,不参与进出场」——那句话在切到方案 C 之后就过期了,
   * 而「进场条件」那一行正是照着它写的,于是长期显示机构那个数。2026-08-20 玻璃
   * 机构 2.09、散户 0.92,页面写着「需达 1(现 2.09)」却又显示无持仓,运营者当场
   * 问「为什么没有触发多单进场条件」。判据见 `../entry-gate.ts`。
   */
  retail: {
    members: MemberLeg[]
    net: number | null
    change: number | null
    /** 反向信号强度。正=散户在减多(反向看涨),负=散户在加多(反向看跌)。 */
    z: number | null
    /** 与机构流向是否同号。实测共振时信号明显更强,背离时基本消失。 */
    resonate: boolean
    trades: boolean
    note: string
  }
  members: MemberLeg[]
  /** 一排合约小窗(DEC-134):近月起、组内还看得到持仓的 5 个合约,逐合约各家持仓。
   *  到期/看不到持仓自动滑出,新合约自动补上。**可选**:旧 JSON 没有。 */
  contracts_panel?: Array<{
    contract: string
    days_left: number
    /** 沉淀资金(DEC-151):持仓×结算×点值×费率;rate=null 时 yi 是名义市值。老 JSON 没有。 */
    sink?: { yi: number; rate: number | null; oi: number } | null
    /** 筹码地图两条带(DEC-152):5 日高低 + 带宽 20%。老 JSON 没有。 */
    zones?: {
      days: number; high: number; low: number
      high_band: [number, number]; low_band: [number, number]
      /** 逐日明细(DEC-152 二改):低点升序 / 高点降序,页面列出来供核口径。老 JSON 没有。 */
      lows?: number[]; highs?: number[]
      last: number | null
    } | null
    members: MemberLeg[]
    /** 五大散户席位对照列(DEC-151)。老 JSON 没有。 */
    retail?: MemberLeg[]
  }>
  /** `manual`/`replace`(DEC-129):运营者点名换人写的那一条,只管到下次重选。 */
  group_log: Array<{ date: string; members: string[]; alpha: Record<string, number | null>
    manual?: boolean; replace?: Record<string, string> }>
  /** 选人方式(DEC-122):rolling=按择时收益滚动重选;fixed=运营者拍板的固定名单(生猪)。
   *  没这个字段的老产物按 rolling 读。 */
  group_mode?: 'fixed' | 'rolling'
  /** 重选切点。`group_log` 只记**换人**,所以阵容连年不变时它会停在很早的日期,
   *  看上去像「三年没重选过」。可选:旧 JSON 没有这个字段。 */
  reselect?: { last: string | null; next: string | null; changed_at: string | null }
  /** 换月反弹提示(DEC-123,只有生猪有):主力剩 ≤dleft_max 日且近 20 日跌 ≥drop_min% →
   *  提示买次主力 X+2。只是提示,不进持仓、不进回测。 */
  roll_bounce?: {
    active: boolean; main: string; days_left: number; drop20: number | null
    dleft_max: number; drop_min: number; next: string; next_px: number | null; since: string
    history: Array<{ date: string; main: string; days_left: number; drop20: number; next: string
      next_px: number | null; next_ret20: number | null; days_seen: number }>
  } | null
  /** 移仓强制流压力表(DEC-136,只有生猪有):散户多头剩仓 → 近月对次主力承压。
   *  只显示不进判据。可选:旧 JSON 与其他品种没有。 */
  roll_pressure?: RollPressureState | null
  /** 单席位跟随第二引擎(DEC-139 焦煤跟华泰;DEC-141 玻璃跟永安)。与主引擎并列、
   *  各管各仓;席位名/统计/丑话全在 payload 里,这里不写死品种。
   *  可选:旧 JSON 与其他品种没有。 */
  seat_follow?: {
    member: string
    side: 'long' | 'short' | null
    net: number | null
    run_days: number | null
    run_ret_pct: number | null
    entry_date: string | null
    entry_px: number | null
    flipped_today: boolean
    history: Array<{ date: string; side: 'long' | 'short'; contract: string
      entry_px: number | null; hold_days: number; ret_pct: number; open: boolean
      /** DEC-150 二改:引擎给的出场日/出场价(=下一段翻向日的同一口开盘);老 JSON 没有。 */
      exit_date?: string | null; exit_px?: number | null }>
    stats: { cum_pct: number; sharpe: number | null; max_dd_pct: number; flips: number
      yearly: Record<string, number> }
    note: string
  } | null
  history: HogTrade[]
  stats: {
    trades: number
    win_rate: number | null
    avg_pct: number | null
    cum_pct: number | null
    short_trades: number
    long_trades: number
    /** 出场原因分布。策略方案页那句「N 笔全部由 X 触发」由它生成,不写死。 */
    exit_reasons: Record<string, number>
  }
  rules: { reselect_months: number; group_k: number; enter: number; stop: number;
           max_hold: number; sig_win: number; long_enabled: boolean
           exit_before_delivery?: number
           /** 'cost' = 机构成本进场(鸡蛋 DEC-112 / 纯碱 DEC-113 / 玻璃 DEC-114);缺省 = 方案 C。 */
           signal_source?: string
           /** 玻璃专用的两条附加(DEC-114),别的品种缺省/假。 */
           cost_need_adding?: boolean; cost_min_age?: number
           /** 'inst' = 机构出场(焦煤,DEC-117);缺省 = 四件套。 */
           exit_mode?: string; cost_unload_max?: number
           /** 'unload_bounce' = 做多只由机构净空且卸仓≥long_unload_min 触发(生猪,DEC-118)。 */
           long_mode?: string; long_unload_min?: number
           /** 做多腿起始日(DEC-124):生猪只从 2026-01-01 起开做多,之前只做空。 */
           long_since?: string | null
           /** 临近交割强平后不许原地续仓,要等新信号(DEC-131)。老产物没有=按开着读。 */
           rearm_after_delivery?: boolean } & Record<string, unknown>
  /** 算出这份信号的那个引擎文件的指纹(DEC-099)。与 `engine.json` 里的比对,
   *  不一致 = 这份信号是旧引擎算的。可选:旧 JSON 没有这个字段。 */
  engine_fingerprint?: string
  /** 顶部风险条。门槛写死在引擎里、数字实算,够不上门槛就是空数组
   *  ——生猪现在 0 条,玻璃 3 条,纯碱 5 条。可选:旧 JSON 没有这个字段。 */
  risk_flags?: Array<{ key: string; text: string }>
  /** 当前主力离散户可交易窗口止点还有多远。窗口止点 = 交割月前月最后一个工作日。
   *  **可选**:前端先于引擎上线,当晚引擎跑过之前线上 JSON 还是上一版没有这个字段,
   *  写成必填会让整页白掉。 */
  delivery?: {
    window_end: string
    days_left: number
    limit: number
    must_exit: boolean
  }
  /** 逐合约战役策略(DEC-133,生猪):多仓并行的持仓清单与逐流观察列表。
   *  **可选**:只有 strategy=campaign 的品种有;其余品种与旧 JSON 都没有。 */
  campaign?: {
    params: Record<string, unknown>
    /** 跨期对冲结构(DEC-137):合格的反向战役共存(空近+多远)。多数时候为空。 */
    pairs?: Array<{ short: string; long: string }>
    positions: HogTrade[]
    watch: Array<{
      contract: string; side: 'long' | 'short'
      retail_net?: number | null; retail_chg5?: number | null; retail_confirm?: boolean
      camp_net: number | null; camp_vwap: number | null
      zone_add: number; batch_cost: number | null; zone_age: number | null
      qualified: boolean; entry_ready: boolean; blocked: string | null
      settle: number; days_left: number
    }>
    qual: { long_pnl_yi: number; short_pnl_yi: number
      long_ok: boolean; short_ok: boolean; share: number }
    note: string
  }
  /** 与「躺着满仓做空」的同口径对比。不给基准,看的人会把熊市 beta 当成策略的本事。 */
  compare: {
    strategy: { cum_pct: number; sharpe: number | null; max_dd_pct: number }
    benchmark: { cum_pct: number; sharpe: number | null; max_dd_pct: number }
    benchmark_name: string
    note: string
  }
  caveats: string[]
}

const data = ref<HogPayload | null>(null)
const error = ref('')
const tab = ref<'today' | 'history' | 'group' | 'rules'>('today')
// 历史信号页的引擎切换(DEC-150,运营者 2026-08-26:「把华泰的历史信号放进去,
// 标注清楚,单独做个第二引擎按钮,两个历史信号就不重叠」)。只有配了 seat_follow
// 的品种(焦煤华泰/玻璃永安)出现按钮;换品种时归位主引擎。
const histEngine = ref<'main' | 'second'>('main')

/** 第二引擎翻转段,**倒序**(最新在上,运营者的历史表铁律)。出场日/价引擎已给
 *  (DEC-150 二改);老 JSON 没有时前端补出场日=下一段翻向日。 */
const secondSorted = computed(() => {
  const h = data.value?.seat_follow?.history ?? []
  return h
    .map((seg, i) => ({
      ...seg,
      exit_date: seg.exit_date ?? h[i + 1]?.date ?? null,
      exit_px: seg.exit_px ?? h[i + 1]?.entry_px ?? null
    }))
    .reverse()
})
// 与主引擎同一套翻页(运营者 2026-08-26:「还有底下的翻页,全部一样」),状态各管各的。
const page2 = ref(1)
const pageSize2 = ref(20)
const secondRows = computed(() => {
  const start = (page2.value - 1) * pageSize2.value
  return secondSorted.value.slice(start, start + pageSize2.value)
})
const total2 = computed(() => secondSorted.value.length)
function changeSize2(size: number) {
  pageSize2.value = size
  page2.value = 1
}

/** 第二引擎的多空分栏统计(与主引擎三卡同构;只算已平段,持有中那段不进)。 */
const secondBySide = computed(() => {
  const closed = (data.value?.seat_follow?.history ?? []).filter((s) => !s.open)
  const calc = (list: typeof closed) => {
    if (!list.length) return null
    const cum = (list.reduce((acc, s) => acc * (1 + s.ret_pct / 100), 1) - 1) * 100
    const win = (list.filter((s) => s.ret_pct > 0).length / list.length) * 100
    const avg = list.reduce((acc, s) => acc + s.ret_pct, 0) / list.length
    return { n: list.length, cum, win, avg }
  }
  return {
    all: calc(closed),
    short: calc(closed.filter((s) => s.side === 'short')),
    long: calc(closed.filter((s) => s.side === 'long'))
  }
})
// 鸡蛋(DEC-112)进场走机构成本信号,策略方案与进场条件的文案都要换一套。
const isCost = computed(() => data.value?.rules.signal_source === 'cost')
// 生猪(DEC-133)整套换成逐合约战役:多仓并行,进出场文案与策略方案页全换。
const isCampaign = computed(() => !!data.value?.campaign)

/**
 * 仓位动作(加多/减多/加空/减空)按**净持仓方向 + 5 日变化方向**说,z 的正负只负责
 * 「反向看涨/看跌」那半句。此前两处都按 z 的正负说动作:2026-08-23 焦煤散户三家
 * 净空 −12,454、5 日变化 −3,018(在加空),页面却写「散户在减多」;机构净多 66,360、
 * 变化 −294(在减多),页面写「机构在加空」。运营者当场指出。
 * 变化为 0 或缺数据时返回 null,调用方退回按 z 说的老话术。
 */
function posAction(net: number | null, change: number | null): string | null {
  if (net === null || change === null || change === 0) return null
  const adding = (net >= 0) === (change > 0)
  return (adding ? '加' : '减') + (net >= 0 ? '多' : '空')
}
const instActionText = computed(() => {
  const s = data.value?.signal
  if (!s) return ''
  const a = posAction(s.net, s.change)
  if (a) return `机构在${a}`
  return (s.z ?? 0) < 0 ? '机构在加空' : '机构在减空/加多'
})
const retailActionText = computed(() => {
  const r = data.value?.retail
  if (!r) return ''
  const view = (r.z ?? 0) > 0 ? '反向看涨' : '反向看跌'
  const a = posAction(r.net, r.change)
  if (a) return `散户在${a} → ${view}`
  return (r.z ?? 0) > 0 ? '散户在减多 → 反向看涨' : '散户在加多 → 反向看跌'
})
// 焦煤(DEC-117)出场走机构出场,策略方案页的出场那一条换文案。
const isInstExit = computed(() => data.value?.rules.exit_mode === 'inst')
const page = ref(1)
// 与金银历史信号页同一套翻页控件(运营者 2026-08-19:后续品种也都用这个)。
// 每页条数可改,所以不是常量。
const pageSize = ref(20)

/**
 * 各家在**逐个合约**上的持仓成本(DEC-134:多合约开战,按合约看)。
 *
 * 不在引擎里重算,直接走净持仓页那条接口(`seats/net-position`)——那套成本引擎
 * 是 Rust 侧算的、有测试盯着,再用 Python 抄一遍就是同一个事实两处维护。
 * 每个面板合约各取一份,并发、失败静默:成本是锦上添花,信号才是主体。
 * 单合约成本列已从「组内各家」撤掉(运营者 2026-08-24:跨合约价差大,单点没意义)。
 */
const costsByContract = ref<Record<string, Record<string, SeatCost>>>({})

onMounted(async () => {
  void loadEngineFingerprint()
  histEngine.value = 'main'   // 换品种回到主引擎视图(DEC-150)
  page2.value = 1
  followPlan.value = null
  followCosts.value = {}
  void loadFollowPlan()
  try {
    // 与金银同一条路:引擎写静态 JSON,nginx 直接服务。带时间戳绕开缓存。
    const res = await fetch(`/smart-money/${FILES[props.instrument]}?t=${Date.now()}`)
    // 文案格式必须是「HTTP <码>」—— fetch-hint 按 /HTTP (\d{3})/ 分流,
    // 原来写「读取失败 403」它永远匹配不到,退回那句含糊的兜底(DEC-101 的教训)。
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    data.value = await res.json()
  } catch (e) {
    error.value = `${e instanceof Error ? e.message : '读取信号数据失败'} — ${failureHint(e)}`
    return
  }
  loadPlanFromPayload(data.value as { follow_plan?: FollowPlan | null } | null)
  const p = data.value
  if (!p?.members.length) return
  const contracts = (p.contracts_panel ?? []).map((c) => c.contract)
  // 成本要机构+散户两列一起取(DEC-151);名单从面板第一窗读,引擎给什么取什么。
  const panelNames = new Set(p.members.map((m) => m.member))
  for (const r of p.contracts_panel?.[0]?.retail ?? []) panelNames.add(r.member)
  await Promise.all(contracts.map(async (contract) => {
    try {
      const { data: net } = await getSeatNetPosition({
        instrument: props.instrument,
        members: [...panelNames],
        contract
      })
      costsByContract.value[contract] =
        Object.fromEntries(net.latest_members.map((m) => [m.member, m]))
    } catch {
      // 该合约成本取不到就不显示,面板照常用
    }
  }))
})

/**
 * 某家在当前主力合约上的净持仓成本。
 *
 * 按它自己那条腿取:净空看空单成本、净多看多单成本。覆盖不全时标出覆盖手数
 * ——那说明有合约的成本不可知(建仓当日无结算价、或数据起点之前就持有),
 * 不能让人以为这个均价覆盖了全部持仓。
 */
/**
 * 筹码地图(DEC-152,2026-08-28 运营者定;**展示级,不进任何判据**)。
 *
 * 运营者原话:「观察每天的波动,看机构席位的空单成本以及多单成本,不管多单和空单,
 * 筹码要优于机构,尽量买到最优筹码位置,提高容错率」。
 *   · 带 = 引擎给的 5 日高/低带(zone_band);高带=空进场/多出场,低带=多进场/空出场。
 *   · **锚 = 机构侧(左列五家)的最优成本**(运营者选定:「空单选机构最优成本」)——
 *     做空成本越高越优取 max、做多成本越低越优取 min;散户列只作对手参考不当锚。
 *   · 优劣 = 现价与锚比:做空现价 ≥ 锚 = 筹码优于机构(卖得比它贵),做多反之。
 * 成本取自净持仓引擎(DEC-143 口径),掉榜/不可知的家自动跳过。
 */
function chipAnchor(contract: string, members: MemberLeg[], side: 'short' | 'long') {
  let best: { member: string; cost: number } | null = null
  for (const m of members) {
    if (!m.on_board) continue
    if (side === 'short' ? m.net >= 0 : m.net <= 0) continue // 只认站在这一边的家
    const c = costsByContract.value[contract]?.[m.member]
    if (!c || c.missing) continue
    const raw = side === 'short' ? c.short_cost : c.long_cost
    if (raw === null) continue
    const v = Number(raw)
    if (!Number.isFinite(v)) continue
    if (!best || (side === 'short' ? v > best.cost : v < best.cost)) {
      best = { member: m.member, cost: v }
    }
  }
  return best
}

/** 现价筹码是否优于机构锚。锚缺失(该向机构全掉榜/成本不可知)时返回 null 不下判断。 */
function chipEdge(last: number | null | undefined, anchor: { cost: number } | null, side: 'short' | 'long') {
  if (last === null || last === undefined || !anchor) return null
  return side === 'short' ? last >= anchor.cost : last <= anchor.cost
}

/** 带的文案。两端相同(两天同高/同低)时只写一个价,别显示 "900~900"。 */
/**
 * 「永安跟随策略」(DEC-154,展示级,**不是下单指令**)。引擎在 pair_fgsa.json 里
 * 按永安当日真实结构等比缩到运营者资金,这里只渲染 + 补各腿的永安持仓成本
 * (成本走净持仓引擎,DEC-143;SA 腿不在本页 payload 里,单独取)。
 */
interface FollowPlan {
  /** opposite/same = 玻纯跨品种版(DEC-154);spread/trend = 跨月版(DEC-168)。 */
  state: 'opposite' | 'same' | 'spread' | 'trend'
  member: string
  capital?: number
  use_pct?: number
  fg_net?: number
  sa_net?: number
  /** 跨月版:两腿手数与比例,以及分腿净值(玻纯版走 fg_net_wan/sa_net_wan)。 */
  long_lots?: number
  short_lots?: number
  ratio?: number
  splits?: Array<{ label: string; wan: number }>
  legs: Array<{ contract: string; instrument: string; side: 'long' | 'short'
    lots: number; px: number; member_net: number; value_wan: number }>
  margin?: number; margin_pct?: number; notional_wan?: number; leverage?: number
  fg_net_wan?: number; sa_net_wan?: number
  net_exposure_wan?: number; net_exposure_pct?: number; net_of_notional_pct?: number
  risk_same?: number; risk_spread?: number
  note: string
}
const followPlan = ref<FollowPlan | null>(null)
/** 各腿的永安持仓成本(键=合约),各腿单独取,不依赖它恰好在五窗里。 */
const followCosts = ref<Record<string, string>>({})

async function loadFollowPlan() {
  // 玻纯那张是**跨品种**方案,材料要两个品种都跑完才合成,所以单独挂在
  // pair_fgsa.json 上;跨月版(DEC-168)两条腿都在同一个品种里,直接躺在本品种
  // 自己的 payload 里,由 loadPlanFromPayload 接。
  if (props.instrument !== 'FG') return
  try {
    const res = await fetch(`/smart-money/pair_fgsa.json?t=${Date.now()}`)
    if (!res.ok) return
    const data = (await res.json()) as { follow_plan?: FollowPlan | null }
    followPlan.value = data.follow_plan ?? null
  } catch {
    return                                       // 取不到就不显示这张卡
  }
  await loadFollowCosts()
}

/** 跨月版:方案就在本品种 payload 里,主数据到位之后调。 */
function loadPlanFromPayload(payload: { follow_plan?: FollowPlan | null } | null) {
  if (props.instrument === 'FG') return          // 玻璃页那张走 pair_fgsa,别互相覆盖
  followPlan.value = payload?.follow_plan ?? null
  void loadFollowCosts()
}

async function loadFollowCosts() {
  const plan = followPlan.value
  if (!plan?.legs?.length) return
  await Promise.all(plan.legs.map(async (lg) => {
    try {
      const { data: net } = await getSeatNetPosition({
        instrument: lg.instrument, members: [plan.member], contract: lg.contract
      })
      // 不标类型:本文件的 MemberLeg 是五窗那个,与接口返回的同名类型不是一回事。
      const m = net.latest_members.find((x) => x.member === plan.member)
      if (!m || m.missing) return
      const raw = lg.side === 'long' ? m.long_cost : m.short_cost
      if (raw !== null) followCosts.value[lg.contract] = Number(raw).toFixed(0)
    } catch {
      // 单腿成本取不到就不显示那一个
    }
  }))
}

/** 散户三家「合计变化」的方向文案(DEC-155):口径同 panelChg,按合计净持仓方向说。 */
const retailTotalChg = computed(() => {
  const r = data.value?.retail
  if (!r || r.change === null || r.change === undefined) return '—'
  if (r.change === 0) return '0 手'
  const mag = fmt(Math.abs(r.change))
  const net = r.net ?? 0
  if (net > 0) return `净多${r.change > 0 ? '+' : '-'}${mag} 手`
  if (net < 0) return `净空${r.change < 0 ? '+' : '-'}${mag} 手`
  return `${r.change > 0 ? '+' : ''}${fmt(r.change)} 手`
})

const band = (b: [number, number]) => (b[0] === b[1] ? `${fmt(b[0])}` : `${fmt(b[0])}~${fmt(b[1])}`)

/** vs 对照表的成本格(DEC-151):数字带 @ 前缀,「掉榜/不可知」原样,没有就空着。 */
function costText(contract: string, name: string): string {
  const t = memberCost(contract, name)
  return t === '—' ? '' : /^\d/.test(t) ? '@' + t : t
}

function memberCost(contract: string, name: string): string {
  const c = costsByContract.value[contract]?.[name]
  if (!c) return '—'
  if (c.missing) return '当日掉榜'
  const short = Number(c.short_lots)
  const long = Number(c.long_lots)
  const [cost, lots, all] = short >= long
    ? [c.short_cost, c.short_cost_lots, c.short_lots]
    : [c.long_cost, c.long_cost_lots, c.long_lots]
  if (cost === null) return '成本不可知'
  const covered = Number(lots)
  const total = Number(all)
  const px = Number(cost).toFixed(0)
  return covered < total ? `${px}(覆盖 ${fmt(covered)} 手)` : px
}

const fmt = (v: number | null | undefined, d = 0) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toLocaleString('zh-CN', {
    minimumFractionDigits: d, maximumFractionDigits: d
  })
const pct = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
/** 期货看盘惯例:涨红跌绿。空单赚钱时价格在跌,但盈亏本身仍按正负着色。 */
const pnlClass = (v: number | null | undefined) =>
  v === null || v === undefined ? '' : v > 0 ? 'red' : v < 0 ? 'green' : ''

const sideText = (s: string) => (s === 'short' ? '做空' : '做多')

/**
 * 规则文案一律**由 payload 生成,不写死**。
 *
 * 上一版把「每 3 个月」「36 笔」直接写进模板,引擎参数改成一年、做多关掉之后,
 * 页面还在说 3 个月和 36 笔——同一个事实两处维护,必然对不上(运营者当场发现)。
 */
const isFixedGroup = computed(() => data.value?.group_mode === 'fixed')
const reselectText = computed(() => {
  const m = data.value?.rules.reselect_months ?? 0
  return m === 12 ? '每年' : m === 1 ? '每月' : `每 ${m} 个月`
})
/** 「N 笔全部由 反向/止损 触发」——原因和笔数都是数出来的。 */
/** 交割倒计时的颜色档:撞线红、还剩不到两倍门槛的黄、其余不上色。
 *  门槛来自 payload,不写死 —— 规则改了文案与配色要跟着改。 */
/** 部署时写下的「当前引擎指纹」。取不到就当没有,不误报。 */
const liveFingerprint = ref<string | null>(null)
async function loadEngineFingerprint() {
  try {
    const res = await fetch(`/smart-money/engine.json?t=${Date.now()}`)
    if (res.ok) liveFingerprint.value = (await res.json())?.fingerprint ?? null
  } catch {
    // 取不到就不判断 —— 宁可不报,也不要报错的
  }
}

/**
 * 这份信号是不是当前引擎算的(DEC-099)。
 *
 * 页面读的是每日任务产出的**静态 JSON**,部署只换代码、不重算 JSON。
 * 2026-08-20 DEC-096(持仓跟合约)上线后就这样过了一夜:代码已上线,页面仍显示
 * 「玻璃 进场 FG2609 / 现价 FG2701」那笔本该被平掉的持仓,而且**看不出来**。
 * 部署现在会自动重算(deploy-futures.yml 的 ENGINE_REFRESH),这一条是兜底。
 *
 * 两边任一为空都不判 —— 旧 JSON 没有这个字段,不能因为缺字段就报警。
 */
const engineStale = computed(() => {
  const a = data.value?.engine_fingerprint
  const b = liveFingerprint.value
  return Boolean(a && b && a !== b)
})

const deliveryClass = computed(() => {
  const d = data.value?.delivery
  if (!d) return ''
  if (d.must_exit) return 'must'
  return d.days_left <= d.limit * 2 ? 'near' : ''
})

/** 把引擎文案里的 **加粗** 转成 <b>。只认这一种记法,其余字符先转义——
 *  文案来自我们自己的引擎,但转义是习惯,别给 v-html 开后门。 */
function mdBold(text: string): string {
  const safe = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return safe.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
}

const exitText = computed(() => {
  const r = data.value?.stats.exit_reasons ?? {}
  const parts = Object.entries(r).map(([k, v]) => `${k} ${v} 笔`)
  const all = Object.keys(r)
  const unused = ['反向', '止损', '持满', '消退'].filter((x) => !all.includes(x))
  const tail = unused.length ? `;${unused.join('、')}至今一次没触发过。` : '。'
  return `实测 ${data.value?.stats.trades ?? 0} 笔的出场分布:${parts.join('、')}${tail}`
})

/**
 * 持仓是否跨越过主力换月。
 *
 * 生猪主力换得勤,而各合约价差最大到 49%——一旦跨了换月,**进场价与现价就不在
 * 同一个合约上**,并排摆着会让人拿它们相减。实盘当前这笔就是:2026-08-04 在
 * LH2609 进场 @10825,现在主力已是 LH2611 报 12485,看着像涨 15%,而真实收益
 * 是 +5.23%(逐日收益累积,换月日用新合约自己的前一日结算价)。必须标出来。
 */
const rolled = computed(() =>
  !!data.value?.position && data.value.position.contract !== data.value.contract)

/**
 * 在榜家数与峰值日不同 —— 这个比例不干净,界面要说出来。**两种情形要分开说**:
 *
 * · `fewer` 今日比峰值日少 → 合计净持仓的下降里混着掉榜,分不清是不是真出货;
 * · `more`  今日比峰值日多 → 峰值那天有人没上榜,真实峰值可能更高,比例偏低。
 *
 * 第一版只写了掉榜那一种,而生猪当前正好是反过来的(今日 5 家、峰值日 4 家),
 * 那句话对它是错的。
 */
const unloadMuddled = computed<'fewer' | 'more' | null>(() => {
  const u = data.value?.institution.unload
  if (!u || u.pct === null || u.legs_at_peak === null || u.legs_now === null) return null
  if (u.legs_now === u.legs_at_peak) return null
  return u.legs_now < u.legs_at_peak ? 'fewer' : 'more'
})

/**
 * **机构合计流向**那张卡里的强度条:机构 z 相对进场门槛的比例,超过就满格。
 *
 * **满格不等于会进场。** 方案 C 下进场比的是共振后的散户信号(见 ../entry-gate.ts),
 * 机构这一路只是共振的另一半。2026-08-20 玻璃机构 2.09 把这条画成满格红条,
 * 而真正被测的散户是 0.92 —— 满格的视觉配上当时那句写错的「进场条件」,
 * 一起把人引向「条件已满足却没进场」。文案已修;这条仍按门槛缩放,
 * 因为它就长在「机构合计流向」卡里、旁边写着「机构在减空/加多」。
 */
const zRatio = computed(() => {
  const z = data.value?.signal.z
  const e = data.value?.signal.enter ?? 1
  if (z === null || z === undefined) return 0
  return Math.min(Math.abs(z) / e, 1)
})

const rows = computed(() => {
  const h = data.value?.history ?? []
  // 按**进场日期**倒序(运营者 2026-08-24 再次指定:最新的在最上面)。
  // campaign 多合约并行让引擎产出顺序按流分组,必须显式排序。
  const sorted = [...h].sort((a, b) => b.entry_date.localeCompare(a.entry_date)
    || a.contract.localeCompare(b.contract))
  const start = (page.value - 1) * pageSize.value
  return sorted.slice(start, start + pageSize.value)
})
const total = computed(() => data.value?.history.length ?? 0)
function changeSize(size: number) {
  pageSize.value = size
  page.value = 1 // 换了每页条数还停在第 7 页,多半就翻到空页了
}

/** 多空各自的统计。做多那条支路要单独摆出来——它是未验证的那条。 */
const bySide = computed(() => {
  const h = data.value?.history ?? []
  const calc = (side: string) => {
    const s = h.filter((t) => t.side === side)
    if (!s.length) return null
    const cum = s.reduce((a, t) => a * (1 + t.ret_pct / 100), 1) - 1
    return {
      n: s.length,
      cum: cum * 100,
      win: (s.filter((t) => t.ret_pct > 0).length / s.length) * 100,
      avg: s.reduce((a, t) => a + t.ret_pct, 0) / s.length
    }
  }
  return { short: calc('short'), long: calc('long') }
})
</script>

<template>
  <div v-if="error" class="err">{{ error }}</div>
  <div v-else-if="!data" class="loading">正在读取信号数据…</div>

  <template v-else>
    <p class="sub">
      跟随机构合计资金流向,每日收盘后自动计算。数据日期 <b>{{ data.data_date }}</b> ·
      计算于 {{ data.computed_at }}
    </p>

    <!-- 风险条(运营者 2026-08-19 要求)。**摆在收益数字前面**——放在下面等于没放。
         条目由引擎按写死的门槛实算,不是按品种硬编码:哪天这个品种真变好了,
         条目会自己消失。 -->
    <!-- 信号过期(DEC-099)。摆在最上面:它一旦出现,下面所有数字都不能信。 -->
    <div v-if="engineStale" class="stale-banner">
      ⚠ <b>这份信号是旧引擎算的</b> —— 引擎已经更新,但这份 JSON 还是更新之前跑出来的,
      下面的持仓、历史与统计都可能与现行规则不符。等下一轮定时任务跑过会自动对齐。
    </div>

    <div v-if="data.risk_flags && data.risk_flags.length" class="risk-banner">
      <div class="risk-head">
        ⚠ 这条曲线不好拿住 —— 先看完这{{ data.risk_flags.length }}条再看收益
      </div>
      <ul>
        <li v-for="f in data.risk_flags" :key="f.key" v-html="mdBold(f.text)"></li>
      </ul>
      <div class="risk-foot">
        仓位按<b>最大回撤</b>定,不是按累计收益定。收益口径见页尾「边界说明」。
      </div>
    </div>

    <!-- 持仓状态条。空仓不渲染,与金银页同一条规矩。 -->
    <div v-if="data.position" class="symbol-strip">
      <div class="strip-row">
        <!-- 合约用**当前主力**:旁边那个价格就是它的,写成进场时的合约会对不上 -->
        <span class="strip-name">{{ data.name }} {{ data.contract }}</span>
        <span class="strip-price" :class="pnlClass(data.position.ret_pct)">
          {{ fmt(data.price, 0) }}
        </span>
        <span class="strip-pct" :class="pnlClass(data.position.ret_pct)">
          {{ pct(data.position.ret_pct) }}
        </span>
        <span class="strip-pill" :class="{ unverified: data.position.side === 'long' }">
          {{ sideText(data.position.side) }}中 {{ data.position.hold_days }} 日
          <template v-if="data.position.side === 'long'"> · 未验证</template>
        </span>
        <span class="strip-metrics">
          <span class="m">
            <i>进场{{ rolled ? `(${data.position.contract})` : '' }}</i>
            <b>{{ fmt(data.position.entry_px, 0) }}</b>
          </span>
          <span class="m"><i>信号强度</i><b>{{ fmt(data.signal.z, 2) }}</b></span>
          <span class="m"><i>机构合计净持仓</i><b>{{ fmt(data.signal.net) }}</b></span>
        </span>
      </div>
    </div>

    <div class="tabbar">
      <div class="tab" :class="{ on: tab === 'today' }" @click="tab = 'today'">今日信号</div>
      <div class="tab" :class="{ on: tab === 'history' }" @click="tab = 'history'">历史信号</div>
      <div class="tab" :class="{ on: tab === 'group' }" @click="tab = 'group'">席位组</div>
      <div class="tab" :class="{ on: tab === 'rules' }" @click="tab = 'rules'">策略方案</div>
    </div>

    <!-- ------------------------------------------------ 今日信号 -->
    <template v-if="tab === 'today'">
      <div class="cards">
        <div class="card">
          <h3>
            <span class="dot" :class="data.position ? (data.position.side === 'short' ? 'green' : 'red') : 'gray'" />
            {{ data.name }} — {{ data.state }}
          </h3>
          <div v-if="data.position" class="big" :class="pnlClass(data.position.ret_pct)">
            {{ pct(data.position.ret_pct) }}
          </div>
          <div v-else class="big gray">无持仓</div>
          <div class="kv"><span class="k">收盘价</span><span class="v">{{ fmt(data.price) }} · {{ data.contract }}</span></div>
          <!-- 交割倒计时(2026-08-19 运营者要求)。2026-08-14 玻璃主力还是 FG2609、
               只剩 11 个交易日,页面对此只字不提 —— 差一天就撞线。 -->
          <div class="kv" v-if="data.delivery">
            <span class="k">可持有</span>
            <span class="v" :class="deliveryClass">
              还剩 {{ data.delivery.days_left }} 个交易日
              <template v-if="data.delivery.must_exit"> · 必须平仓</template>
            </span>
          </div>
          <template v-if="data.position">
            <div class="kv"><span class="k">进场</span>
              <span class="v">{{ data.position.entry_date }} @ {{ fmt(data.position.entry_px) }}
                <template v-if="rolled">· {{ data.position.contract }}</template>
              </span></div>
            <div class="kv"><span class="k">方向</span><span class="v">{{ sideText(data.position.side) }}</span></div>
            <div class="kv"><span class="k">持有</span><span class="v">{{ data.position.hold_days }} 个交易日</span></div>
          </template>
          <p v-if="rolled" class="note">
            这笔持仓跨过了主力换月:进场在 {{ data.position!.contract }},上面那个收盘价是
            {{ data.contract }} 的。**两个价格不在同一个合约上,不要相减**——
            收益按逐日累积算(换月日用新合约自己的前一日结算价),生猪各合约价差
            最大到 49%,相减会得出一个完全错误的数。
          </p>
          <div v-else-if="!data.position" class="kv">
            <span class="k">进场条件</span>
            <span class="v">
              <b v-if="data.signal.entry_side" :class="data.signal.entry_side === 'long' ? 'red' : 'green'">
                {{ data.signal.entry_side === 'long' ? '⚡ 做多' : '⚡ 做空' }}
              </b>
              {{ entryGateText(data) }}
            </span>
          </div>
        </div>

        <div class="card">
          <h3>机构合计流向</h3>
          <div class="meter">
            <div class="meter-bar">
              <div class="meter-fill" :class="(data.signal.z ?? 0) < 0 ? 'green' : 'red'"
                   :style="{ width: `${zRatio * 100}%` }" />
            </div>
            <div class="meter-label">
              <span>{{ instActionText }}</span>
              <b>{{ fmt(data.signal.z, 2) }}</b>
            </div>
          </div>
          <div class="kv"><span class="k">合计净持仓</span><span class="v">{{ fmt(data.signal.net) }} 手</span></div>
          <div v-if="data.institution.unload && data.institution.unload.pct !== null" class="kv">
            <span class="k">已卸掉</span>
            <span class="v">
              {{ (data.institution.unload.pct * 100).toFixed(0) }}%
              <i class="peak">峰值 {{ fmt(data.institution.unload.peak_net) }} 手
                @{{ (data.institution.unload.peak_date || '').slice(5) }}</i>
            </span>
          </div>
          <p v-if="unloadMuddled" class="unload-warn">
            峰值日在榜 {{ data.institution.unload!.legs_at_peak }} 家、今日
            {{ data.institution.unload!.legs_now }} 家 ——
            <template v-if="unloadMuddled === 'fewer'">
              <b>这个降幅分不清是出货还是掉榜</b>。掉出前二十不等于减仓,
              合计净持仓会跟着降,而他可能一手没动。
            </template>
            <template v-else>
              <b>峰值那天有人没上榜,真实峰值可能更高</b>,这个比例是偏低的估计。
            </template>
          </p>
          <div class="kv"><span class="k">{{ data.signal.win }} 日变化</span>
            <span class="v" :class="pnlClass(data.signal.change)">{{ fmt(data.signal.change) }} 手</span></div>
          <div class="kv"><span class="k">建议仓位强度</span><span class="v">{{ fmt(data.signal.suggested_position, 2) }}</span></div>
          <div class="kv">
            <span class="k">机构方向</span>
            <span class="v" :class="data.institution.side === 'net_long' ? 'red' : 'green'">
              {{ data.institution.side === 'net_long' ? '净多' : '净空' }}
              <template v-if="data.institution.just_flipped_long">· 刚转多</template>
            </span>
          </div>
          <p class="note">
            信号读的是**这几家合起来往哪个方向调仓**,不是谁的仓位大。
            负值=加空、正值=减空或加多。
          </p>
        </div>

        <div class="card">
          <h3>组内各家(共 {{ data.members.length }} 家)</h3>
          <div v-for="m in data.members" :key="m.member" class="kv">
            <span class="k">{{ m.member }}</span>
            <span class="v">
              <template v-if="m.on_board">
                {{ fmt(m.net) }} 手
                <!-- DEC-153:与五窗同一套方向表述(净多+X/净空+X),净数会把加空读成在减。
                     口径不同仍保留:这里是 sig_win 日变化,五窗是较昨日(DEC-146)。 -->
                <span v-if="panelChg(m)" :class="pnlClass(m.change ?? 0)">
                  ({{ panelChg(m) }})
                </span>
              </template>
              <span v-else class="gray">当日未上榜</span>
            </span>
          </div>
          <p class="note">
            <template v-if="isFixedGroup">席位组是运营者拍板的**固定名单**(DEC-122),不滚动重选。</template>
            <template v-else>席位组{{ reselectText }}按历史择时收益重选一次,不是固定名单。</template>
            这是**全品种合约合计**;括号=较 {{ data.signal.win }} 日前变化
            (净多+X=净多增了 X,净空+X=净空增了 X;下面那排小窗是**较昨日**,口径有意不同)。
            逐合约的持仓与成本看下面那排小窗(多合约开战,按合约看)。
          </p>
        </div>
      </div>

      <!-- 第二引擎:单席位跟随(DEC-139 焦煤跟华泰 / DEC-141 玻璃跟永安)。与主引擎并列,各管各仓。 -->
      <div v-if="data.seat_follow" class="cards">
        <div class="card wide">
          <h3>
            第二引擎 · 跟{{ data.seat_follow.member }}
            <span v-if="data.seat_follow.side" class="badge" :class="data.seat_follow.side === 'short' ? 'ok' : 'warn'">
              {{ sideText(data.seat_follow.side) }}中 第 {{ data.seat_follow.run_days }} 日
            </span>
          </h3>
          <p v-if="data.seat_follow.flipped_today" class="caveat-box flip">
            <b>⚡ {{ data.seat_follow.member }} 已翻向 {{ sideText(data.seat_follow.side ?? 'short') }} —— 次日开盘反手(本引擎唯一的操作时点)。</b>
          </p>
          <div class="kv"><span class="k">{{ data.seat_follow.member }}净持仓</span>
            <span class="v" :class="(data.seat_follow.net ?? 0) > 0 ? 'red' : 'green'">{{ fmt(data.seat_follow.net) }} 手</span></div>
          <div class="kv"><span class="k">本段进场</span>
            <span class="v">{{ data.seat_follow.entry_date }} @ {{ fmt(data.seat_follow.entry_px) }}</span></div>
          <div class="kv"><span class="k">本段浮动</span>
            <span class="v" :class="pnlClass(data.seat_follow.run_ret_pct)">{{ pct(data.seat_follow.run_ret_pct) }}</span></div>
          <div class="kv"><span class="k">回放(扣成本)</span>
            <span class="v">累计 {{ pct(data.seat_follow.stats.cum_pct) }} · 夏普 {{ data.seat_follow.stats.sharpe }}
              · 回撤 {{ pct(data.seat_follow.stats.max_dd_pct) }} · 翻转 {{ data.seat_follow.stats.flips }} 次</span></div>
          <div class="kv"><span class="k">逐年</span>
            <span class="v">
              <span v-for="(r, y) in data.seat_follow.stats.yearly" :key="y" class="chip">
                {{ y }} <i :class="pnlClass(r)">{{ pct(r) }}</i></span>
            </span></div>
          <div class="roll-hist">
            <span class="gray">最近翻转:</span>
            <span v-for="h in data.seat_follow.history.slice(-8)" :key="h.date" class="chip">
              {{ h.date.slice(5) }} {{ sideText(h.side) }} {{ h.hold_days }}日
              <i :class="pnlClass(h.ret_pct)">{{ pct(h.ret_pct) }}</i>
              <span v-if="h.open" class="gray">(进行中)</span>
            </span>
          </div>
          <p class="note" v-html="mdBold(data.seat_follow.note)"></p>
        </div>
      </div>

      <!-- 一排合约小窗(DEC-134,运营者 2026-08-24):多合约开战,逐合约看
           各家持仓与该合约上的成本。到期/看不到持仓自动滑出,恒 5 个。
           金银不在此组件,天然不受影响。 -->
      <!-- DEC-151(2026-08-28 样式经运营者确认):全部活跃合约开窗、一行一窗做宽,
           机构 5 家 vs 五大散户对照(散户同款净多/净空变化),窗头挂沉淀资金。 -->
      <div v-if="data.contracts_panel && data.contracts_panel.length" class="panel-row">
        <!-- 永安跟随策略(DEC-154):与合约窗同宽,排在第一格;随永安持仓每日自动变。 -->
        <div v-if="followPlan" class="card panel-card wide">
          <h3>
            {{ followPlan.member }}跟随策略<template v-if="followPlan.ratio">（跨月 1 : {{ followPlan.ratio }}）</template>
            <span class="panel-sink" v-if="followPlan.capital">
              总资金 <b>{{ (followPlan.capital / 10000).toFixed(0) }} 万</b>
              <span class="cost">· 保证金 {{ followPlan.use_pct }}%</span>
            </span>
          </h3>
          <template v-if="followPlan.state === 'opposite' || followPlan.state === 'spread'">
            <table class="panel-vs">
              <tr v-for="lg in followPlan.legs" :key="lg.contract">
                <td class="k"><span class="chip-side" :class="lg.side">{{ lg.side === 'long' ? '多' : '空' }}</span> {{ lg.contract }}</td>
                <td class="num"><b>{{ lg.lots }} 手</b></td>
                <td class="num cost">@{{ lg.px }}</td>
                <!-- 席位名读 followPlan.member,**不能写死**:这张卡从 DEC-168 起也给
                     东证跑焦煤,写死「永安」会让东证的持仓顶着永安的名字(2026-08-31
                     上线后 Chrome 实机第一眼就看出来了)。 -->
                <td class="num cost">{{ followPlan.member.replace('期货', '') }} {{ fmt(lg.member_net) }}<template v-if="followCosts[lg.contract]"> @{{ followCosts[lg.contract] }}</template></td>
              </tr>
            </table>
            <div class="chip-map">
              <div class="chip-line">
                保证金 <b>{{ ((followPlan.margin ?? 0) / 10000).toFixed(1) }} 万</b>({{ followPlan.margin_pct }}%)
                <span class="sep">·</span> 名义 {{ followPlan.notional_wan }} 万 <span class="sep">·</span> 杠杆 {{ followPlan.leverage }} 倍
              </div>
              <!-- 玻纯版是两个品种各自的净值;跨月版是远月腿/近月腿。引擎给 splits
                   就照 splits 渲染,不在这里判品种 —— 加第三个品种时前端不必再改。 -->
              <div class="chip-line" v-if="followPlan.splits?.length">
                <template v-for="(sp, i) in followPlan.splits" :key="sp.label">
                  <span v-if="i" class="sep">·</span>
                  {{ sp.label }} <span :class="pnlClass(sp.wan)">{{ sp.wan }} 万</span>
                </template>
              </div>
              <div class="chip-line" v-else>
                玻璃净 <span :class="pnlClass(followPlan.fg_net_wan ?? 0)">{{ followPlan.fg_net_wan }} 万</span>
                <span class="sep">·</span> 纯碱净 <span :class="pnlClass(followPlan.sa_net_wan ?? 0)">{{ followPlan.sa_net_wan }} 万</span>
              </div>
              <div class="chip-line">
                <b>风险敞口 {{ followPlan.net_exposure_wan }} 万</b>
                <span class="cost">(本金 {{ followPlan.net_exposure_pct }}% · 总名义 {{ followPlan.net_of_notional_pct }}%)</span>
              </div>
              <div class="chip-line gray">
                单日:同涨跌 1% → {{ followPlan.risk_same }} 元 · 价差反向各 1% → ±{{ followPlan.risk_spread }} 元
              </div>
            </div>
          </template>
          <p class="note" v-html="mdBold(followPlan.note)"></p>
        </div>
        <div v-for="c in data.contracts_panel" :key="c.contract" class="card panel-card wide">
          <h3>
            {{ c.contract }}
            <span class="panel-days" :class="{ near: c.days_left <= (data.rules.exit_before_delivery ?? 10) * 2 }">
              剩 {{ c.days_left }} 日
            </span>
            <span v-if="c.sink" class="panel-sink">
              {{ c.sink.rate ? '沉淀资金' : '名义市值' }} <b>{{ c.sink.yi }} 亿</b>
              <span class="cost">{{ c.sink.rate ? `(持仓×现价×点值×${(c.sink.rate * 100).toFixed(0)}%,推算)` : '(未配费率)' }}</span>
            </span>
          </h3>
          <table class="panel-vs">
            <tr v-for="(m, i) in c.members" :key="m.member">
              <td class="k">{{ m.member.slice(0, 4) }}</td>
              <td class="num">
                <template v-if="m.on_board">
                  <span :class="m.net > 0 ? 'red' : m.net < 0 ? 'green' : ''">{{ fmt(m.net) }}</span>
                  <span v-if="panelChg(m)" :class="pnlClass(m.change ?? 0)"> ({{ panelChg(m) }})</span>
                </template>
                <span v-else class="gray">未上榜</span>
              </td>
              <td class="num cost">{{ m.on_board ? costText(c.contract, m.member) : '' }}</td>
              <td class="vs-cell">vs</td>
              <template v-if="c.retail?.[i]">
                <td class="k">{{ c.retail[i].member.slice(0, 4) }}</td>
                <td class="num">
                  <template v-if="c.retail[i].on_board">
                    <span :class="c.retail[i].net > 0 ? 'red' : c.retail[i].net < 0 ? 'green' : ''">{{ fmt(c.retail[i].net) }}</span>
                    <span v-if="panelChg(c.retail[i])" :class="pnlClass(c.retail[i].change ?? 0)"> ({{ panelChg(c.retail[i]) }})</span>
                  </template>
                  <span v-else class="gray">未上榜</span>
                </td>
                <td class="num cost">{{ c.retail[i].on_board ? costText(c.contract, c.retail[i].member) : '' }}</td>
              </template>
              <template v-else><td class="k"></td><td class="num"></td><td class="num cost"></td></template>
            </tr>
          </table>
          <!-- 筹码地图(DEC-152,展示级):高带=空进场/多出场,低带=多进场/空出场;
               锚=机构侧最优成本,✓=现价筹码优于机构。仓位由运营者自定,这里不标层数。 -->
          <div v-if="c.zones" class="chip-map">
            <div class="chip-line">
              <span class="chip-side short">空</span>
              <span>进场 <b>{{ band(c.zones.high_band) }}</b></span>
              <span class="sep">·</span>
              <span>出场 {{ band(c.zones.low_band) }}</span>
              <template v-if="chipAnchor(c.contract, c.members, 'short')">
                <span class="sep">·</span>
                <span class="anchor">机构最优空 @{{ chipAnchor(c.contract, c.members, 'short')!.cost.toFixed(0) }}
                  <i>{{ chipAnchor(c.contract, c.members, 'short')!.member.slice(0, 2) }}</i></span>
                <span :class="chipEdge(c.zones.last, chipAnchor(c.contract, c.members, 'short'), 'short') ? 'edge-ok' : 'edge-no'">
                  {{ chipEdge(c.zones.last, chipAnchor(c.contract, c.members, 'short'), 'short') ? '✓ 现价筹码更优' : '✗ 不如机构' }}
                </span>
              </template>
            </div>
            <div class="chip-line">
              <span class="chip-side long">多</span>
              <span>进场 <b>{{ band(c.zones.low_band) }}</b></span>
              <span class="sep">·</span>
              <span>出场 {{ band(c.zones.high_band) }}</span>
              <template v-if="chipAnchor(c.contract, c.members, 'long')">
                <span class="sep">·</span>
                <span class="anchor">机构最优多 @{{ chipAnchor(c.contract, c.members, 'long')!.cost.toFixed(0) }}
                  <i>{{ chipAnchor(c.contract, c.members, 'long')!.member.slice(0, 2) }}</i></span>
                <span :class="chipEdge(c.zones.last, chipAnchor(c.contract, c.members, 'long'), 'long') ? 'edge-ok' : 'edge-no'">
                  {{ chipEdge(c.zones.last, chipAnchor(c.contract, c.members, 'long'), 'long') ? '✓ 现价筹码更优' : '✗ 不如机构' }}
                </span>
              </template>
            </div>
            <div class="chip-line gray">
              现价 {{ fmt(c.zones.last) }} · 近 {{ c.zones.days }} 日
              <template v-if="c.zones.lows?.length">
                低 {{ c.zones.lows.map((x) => fmt(x)).join('·') }} · 高 {{ c.zones.highs?.map((x) => fmt(x)).join('·') }}
              </template>
              <template v-else>{{ fmt(c.zones.low) }}~{{ fmt(c.zones.high) }}</template>
            </div>
          </div>
        </div>
      </div>
      <p v-if="data.contracts_panel && data.contracts_panel.length" class="note panel-note">
        每窗:左=跟踪席位,右=五大散户席位(东方财富/方正中期/徽商/平安/中信建投),
        各给净持仓(正红=净多,负绿=净空)、较昨日变化(净多+X=净多增了 X,净空+X=净空增了 X)、
        @净持仓成本(推算,按结算价推,不是成交均价)。窗头沉淀资金=全市场持仓×现价×点值×保证金率
        (费率为品种配置的近似,非交易所实时;未配费率的品种给名义市值)。
        **全部活跃合约开窗**,到期自动滑出、新合约自动补上。
      </p>

      <!-- 逐合约战役(DEC-133,生猪):多仓并行 —— 顶部状态条只显示最新一笔,
           全部持仓与逐合约观察列表都在这里。 -->
      <div v-if="isCampaign && data.campaign" class="cards">
        <div class="card wide">
          <h3>战役持仓({{ data.campaign.positions.length }} 笔并行)</h3>
          <p v-if="data.campaign.pairs && data.campaign.pairs.length" class="note pair-note">
            <b>当前呈跨期对冲结构:</b>
            <span v-for="pr in data.campaign.pairs" :key="pr.short + pr.long" class="chip">
              空 {{ pr.short }} + 多 {{ pr.long }}
            </span>
            —— 两腿都是合格聪明钱战役,方向风险部分对消;逐腿仍各按自己的阵营出场。
          </p>
          <table v-if="data.campaign.positions.length" class="tbl">
            <thead><tr><th>合约</th><th>方向</th><th>进场</th><th class="num">进场价</th>
              <th class="num">批次成本</th><th class="num">浮动收益</th><th class="num">机构已卸</th>
              <th>散户接盘</th></tr></thead>
            <tbody>
              <tr v-for="t in data.campaign.positions" :key="t.contract + t.side">
                <td>{{ t.contract }}<span v-if="t.rolled_from" class="units-badge" :title="`交割纪律出场后自 ${t.rolled_from} 接力(机构无出货信号,DEC-147)`">接力</span></td>
                <td><span class="side" :class="t.side">{{ sideText(t.side) }}</span></td>
                <td>
                  {{ t.entry_date }}
                  <div v-if="(t.units ?? 1) > 1" class="units-detail">
                    <span v-for="e in t.entries" :key="e.date">{{ e.date.slice(5) }}@{{ fmt(e.px) }}</span>
                  </div>
                </td>
                <td class="num">{{ fmt(t.entry_px) }}<span v-if="(t.units ?? 1) > 1" class="units-badge">均·{{ t.units }}批</span></td>
                <td class="num">{{ fmt(t.batch_cost) }}</td>
                <td class="num" :class="pnlClass(t.ret_pct)">{{ pct(t.ret_pct) }}</td>
                <td class="num">
                  {{ t.unload_pct === null || t.unload_pct === undefined ? '—' : `${(t.unload_pct * 100).toFixed(0)}%` }}
                  <span class="gray">/30%走</span>
                </td>
                <td>
                  进场{{ t.retail_confirm ? '✓' : '✗' }}
                  <template v-if="t.retail_now">
                    · 现{{ t.retail_now.opposite_adding ? '仍在接 ✓' : '未接' }}
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="kv"><span class="v gray">当前无持仓</span></div>
          <h3 class="watch-head">逐合约观察</h3>
          <table class="tbl">
            <thead><tr><th>合约</th><th>方向</th><th class="num">阵营净持仓</th>
              <th class="num">区间累计加仓</th><th class="num">批次成本</th>
              <th class="num">散户(对面)</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="w in data.campaign.watch" :key="w.contract + w.side">
                <td>{{ w.contract }}</td>
                <td><span class="side" :class="w.side">{{ sideText(w.side) }}</span></td>
                <td class="num">{{ fmt(w.camp_net) }}</td>
                <td class="num">{{ fmt(w.zone_add) }}</td>
                <td class="num">{{ fmt(w.batch_cost) }}</td>
                <td class="num">
                  {{ fmt(w.retail_net) }}
                  <span v-if="w.retail_chg5 !== null && w.retail_chg5 !== undefined" :class="pnlClass(w.retail_chg5)">
                    ({{ (w.retail_chg5 ?? 0) >= 0 ? '+' : '' }}{{ fmt(w.retail_chg5) }})</span>
                  <b v-if="w.retail_confirm" class="green">接盘✓</b>
                </td>
                <td>
                  <b v-if="w.entry_ready" :class="w.side === 'long' ? 'red' : 'green'">⚡ 可进场(结算 {{ fmt(w.settle) }} 已到批次成本)</b>
                  <template v-else>{{ w.blocked }}</template>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="note">
            聪明钱资格(份额判据,需 ≥ 对侧 {{ (data.campaign.qual.share * 100).toFixed(0) }}%):
            空头人格历史战役 {{ data.campaign.qual.short_pnl_yi >= 0 ? '+' : '' }}{{ data.campaign.qual.short_pnl_yi }} 亿
            {{ data.campaign.qual.short_ok ? '✓ 可跟' : '✗ 不可跟' }} ·
            多头人格 {{ data.campaign.qual.long_pnl_yi >= 0 ? '+' : '' }}{{ data.campaign.qual.long_pnl_yi }} 亿
            {{ data.campaign.qual.long_ok ? '✓ 可跟' : '✗ 不可跟' }}。
            {{ data.campaign.note }}
            散户接盘确认(DEC-138,只当仓位分级不当开关):进场时散户 5 日在向对面
            加仓的战役,历史单笔均值 +4.96%/胜 68%;未接盘的 +0.65%/胜 52%(逐年 4/4 同向,
            t=2.29)。接盘 ✓ = 正常仓/可跟批;✗ = 轻仓,仓位自定。
          </p>
        </div>
      </div>

      <!-- 散户反向:与机构流向并列的第二个维度。它们经常指相反方向,
           所以摆在一起看才有意义——共振时信号最强,背离时基本没信息。 -->
      <div class="cards">
        <div class="card">
          <h3>
            散户反向
            <span class="badge" :class="data.retail.resonate ? 'ok' : 'warn'">
              {{ data.retail.resonate ? '与机构共振' : '与机构背离' }}
            </span>
          </h3>
          <div class="meter">
            <div class="meter-bar">
              <div class="meter-fill" :class="(data.retail.z ?? 0) > 0 ? 'red' : 'green'"
                   :style="{ width: `${Math.min(Math.abs(data.retail.z ?? 0), 2) / 2 * 100}%` }" />
            </div>
            <div class="meter-label">
              <span>{{ retailActionText }}</span>
              <b>{{ fmt(data.retail.z, 2) }}</b>
            </div>
          </div>
          <div class="kv"><span class="k">三家合计净持仓</span>
            <span class="v" :class="(data.retail.net ?? 0) > 0 ? 'red' : 'green'">
              {{ fmt(data.retail.net) }} 手</span></div>
          <div class="kv"><span class="k">{{ data.signal.win }} 日变化</span>
            <span class="v" :class="pnlClass(-(data.retail.change ?? 0))">{{ retailTotalChg }}</span></div>
          <div v-for="m in data.retail.members" :key="m.member" class="kv">
            <span class="k">　{{ m.member }}</span>
            <span class="v">
              <template v-if="m.on_board">{{ fmt(m.net) }} 手
                <!-- DEC-155:与五窗/组内各家同一套方向表述。**颜色仍反着**(散户加多=看跌),
                     只有文案改成净多±/净空±,别把这里的 pnlClass(-m.change) 一起改了。 -->
                <span v-if="panelChg(m)" :class="pnlClass(-(m.change ?? 0))">
                  ({{ panelChg(m) }})</span>
              </template>
              <span v-else class="gray">当日未上榜</span>
            </span>
          </div>
          <p class="note">{{ data.retail.note }}</p>
        </div>
      </div>

      <!-- 换月反弹提示(DEC-123,生猪专用,运营者 2026-08-23 拍板「直接做」):
           主力剩 ≤22 日且近 20 日跌 ≥5% → 买次主力 X+2、移动止盈出场。依据只有 2026 年
           3 次触发(2 对 1 错),是按磨底年判断开的门,不是验证 —— 字面上必须写清。 -->
      <div v-if="data.roll_bounce" class="caveat-box roll" :class="{ on: data.roll_bounce.active }">
        <template v-if="data.roll_bounce.active">
          <b>换月反弹提示:主力 {{ data.roll_bounce.main }} 只剩 {{ data.roll_bounce.days_left }} 个交易日、
          近 20 日跌 {{ data.roll_bounce.drop20 }}% —— 到期前被砸狠了。</b>
          按磨底年的判断,这时买**次主力 {{ data.roll_bounce.next }}**
          (结算 {{ fmt(data.roll_bounce.next_px) }}),移动止盈出场,不拿到期。
        </template>
        <template v-else>
          <b>换月反弹提示(未触发)</b>:主力 {{ data.roll_bounce.main }} 剩 {{ data.roll_bounce.days_left }} 个交易日
          (要 ≤{{ data.roll_bounce.dleft_max }})、近 20 日 {{ data.roll_bounce.drop20 ?? '—' }}%
          (要 ≤−{{ data.roll_bounce.drop_min }}%)。触发时买次主力 {{ data.roll_bounce.next }}。
        </template>
        <div class="roll-hist">
          <span class="gray">{{ data.roll_bounce.since.slice(0, 4) }} 年以来触发:</span>
          <span v-for="h in data.roll_bounce.history" :key="h.date" class="chip">
            {{ h.date }} {{ h.main }}剩{{ h.days_left }}日跌{{ h.drop20 }}% → 买{{ h.next }}
            <i :class="pnlClass(h.next_ret20)">{{ h.next_ret20 === null ? '—' : pct(h.next_ret20) }}</i>
            <span v-if="h.days_seen < 20" class="gray">({{ h.days_seen }}日)</span>
          </span>
          <span v-if="!data.roll_bounce.history.length" class="gray">无</span>
        </div>
        <p class="note">依据只有 2026 年这几次,不是全样本验证(DEC-123):磨底年到期前被砸狠的周期
          次主力有一轮反弹,砸得温和的周期没有。只是提示,不进系统持仓、不算进回测;磨底年过去要回头关掉。</p>
      </div>
      <!-- 移仓压力表(DEC-136,生猪专用):散户多头剩仓 → 近月承压。只是背景。 -->
      <div v-if="data.roll_pressure" class="caveat-box roll" :class="{ on: rollPressureHint(data.roll_pressure).on }">
        <b>移仓压力(散户强制流):</b>{{ rollPressureHint(data.roll_pressure).text }}
        <div class="roll-hist">
          <span class="gray">历届锚点(剩≤{{ data.roll_pressure.anchor }}日时散户剩仓 → 其后价差变动):</span>
          <span v-for="h in data.roll_pressure.history.slice(-8)" :key="h.main" class="chip">
            {{ h.main }} {{ h.retail_net.toLocaleString('zh-CN') }}手
            <i :class="pnlClass(h.spread_move_pct)">{{ h.spread_move_pct === null ? '—' : pct(h.spread_move_pct) }}</i>
          </span>
        </div>
        <p class="note" v-html="mdBold(data.roll_pressure.note)"></p>
      </div>

      <!-- 这三种提示都必须在首屏,不能藏进策略方案页 -->
      <div v-if="data.institution.just_flipped_long" class="caveat-box flip">
        <b>机构合计净持仓刚转为净多。</b>
        这是你要盯的那个拐点,但**系统不会因此进场**——样本里它出现过 14 天
        (集中在 2025-07),之后 20 日主力仍平均跌 1.18%,最好一次只有 +0.61%。
        转多意味着跌得慢,不意味着会涨。要不要手动做多,自己判断。
      </div>
      <div v-else-if="data.institution.long_signal_now && !data.institution.long_enabled"
           class="caveat-box">
        <b>机构在减空(信号强度已过门槛),但做多支路是关闭的,所以不进场。</b>
        回测里多头 15 笔逐笔累计 −1.5%、均值 −0.02%,等于抛硬币;关掉后夏普
        1.96 → 2.39。注意机构此刻仍是**净空 {{ fmt(Math.abs(data.institution.net ?? 0)) }} 手**,
        「减空」不等于「转多」。
      </div>
      <div v-else-if="data.position?.side === 'long'" class="caveat-box">
        <b>当前是做多持仓,而做多这条支路没有数据背书。</b>
        回测里多头 15 笔逐笔累计 −1.5%,请按未验证规则对待。
      </div>
    </template>

    <!-- ------------------------------------------------ 历史信号 -->
    <!-- 主/第二引擎两套历史各占一屏,按钮互切,不重叠(DEC-150)。 -->
    <template v-else-if="tab === 'history' && histEngine === 'second' && data.seat_follow">
      <!-- 与主引擎同规格(DEC-150 二改,运营者:「不要少做…全部一样」):三卡+对比+全量表+翻页。 -->
      <div class="cards">
        <div v-if="secondBySide.all" class="card">
          <h3>全部 {{ secondBySide.all.n }} 段<span class="badge ok">跟{{ data.seat_follow.member.slice(0, 2) }}翻转段</span></h3>
          <div class="kv"><span class="k">累计(毛)</span><span class="v" :class="pnlClass(secondBySide.all.cum)">{{ pct(secondBySide.all.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ secondBySide.all.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单段均值</span><span class="v">{{ pct(secondBySide.all.avg) }}</span></div>
        </div>
        <div v-if="secondBySide.short" class="card">
          <h3>做空 {{ secondBySide.short.n }} 段</h3>
          <div class="kv"><span class="k">累计(毛)</span><span class="v" :class="pnlClass(secondBySide.short.cum)">{{ pct(secondBySide.short.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ secondBySide.short.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单段均值</span><span class="v">{{ pct(secondBySide.short.avg) }}</span></div>
        </div>
        <div v-if="secondBySide.long" class="card">
          <h3>做多 {{ secondBySide.long.n }} 段</h3>
          <div class="kv"><span class="k">累计(毛)</span><span class="v" :class="pnlClass(secondBySide.long.cum)">{{ pct(secondBySide.long.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ secondBySide.long.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单段均值</span><span class="v">{{ pct(secondBySide.long.avg) }}</span></div>
        </div>
      </div>

      <div class="card wide compare">
        <h3>
          第二引擎 · 跟{{ data.seat_follow.member }} — 回放(扣成本)
          <button class="eng-btn" @click="histEngine = 'main'">◂ 返回主引擎</button>
        </h3>
        <table class="cmp">
          <thead><tr><th></th><th class="num">累计</th><th class="num">夏普</th><th class="num">最大回撤</th><th class="num">翻转</th></tr></thead>
          <tbody>
            <tr>
              <td>跟{{ data.seat_follow.member.slice(0, 2) }}(单跑)</td>
              <td class="num" :class="pnlClass(data.seat_follow.stats.cum_pct)">{{ pct(data.seat_follow.stats.cum_pct) }}</td>
              <td class="num"><b>{{ data.seat_follow.stats.sharpe }}</b></td>
              <td class="num"><b>{{ pct(data.seat_follow.stats.max_dd_pct) }}</b></td>
              <td class="num">{{ data.seat_follow.stats.flips }} 次</td>
            </tr>
          </tbody>
        </table>
        <div class="chips" style="margin-top:6px">
          <span v-for="(v, y) in data.seat_follow.stats.yearly" :key="y" class="chip">
            {{ y }} <b :class="pnlClass(v)">{{ pct(v) }}</b></span>
        </div>
        <p class="note">{{ data.seat_follow.note }}</p>
      </div>

      <table class="tbl">
        <thead>
          <tr><th>方向</th><th>翻向日(进场)</th><th>出场(下一次翻向)</th><th>合约</th>
            <th class="num">进场价</th><th class="num">出场价</th><th class="num">段收益</th><th class="num">持有</th></tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in secondRows" :key="i">
            <td><span class="side" :class="s.side">{{ sideText(s.side) }}</span></td>
            <td>{{ s.date }}</td>
            <td>{{ s.exit_date ?? (s.open ? '持有中' : '—') }}</td>
            <td>{{ s.contract }}</td>
            <td class="num">{{ fmt(s.entry_px) }}</td>
            <td class="num">{{ fmt(s.exit_px) }}</td>
            <td class="num" :class="pnlClass(s.ret_pct)">{{ pct(s.ret_pct) }}</td>
            <td class="num">{{ s.hold_days }} 日</td>
          </tr>
        </tbody>
      </table>
      <div class="pager">
        <el-pagination
          v-model:current-page="page2"
          :page-size="pageSize2"
          :page-sizes="[10, 20, 30, 50]"
          :total="total2"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @size-change="changeSize2"
        />
      </div>
      <p class="note">
        {{ data.seat_follow.member }}的翻转段全按**净持仓方向翻向日**切分,出场与下一段进场是
        翻向次日开盘的**同一口价**(反手),没有别的出场规则。表内段收益为毛收益;
        「回放(扣成本)」那行统计为扣成本口径(0.1%/翻转)。全历史 {{ total2 }} 段全部在此,可翻页。
      </p>
    </template>
    <template v-else-if="tab === 'history'">
      <div class="cards">
        <div class="card">
          <h3>全部 {{ data.stats.trades }} 笔</h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(data.stats.cum_pct)">{{ pct(data.stats.cum_pct) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ data.stats.win_rate }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(data.stats.avg_pct) }}</span></div>
        </div>
        <div v-if="bySide.short" class="card">
          <h3>做空 {{ bySide.short.n }} 笔<span class="badge ok">有回测支撑</span></h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(bySide.short.cum)">{{ pct(bySide.short.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ bySide.short.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(bySide.short.avg) }}</span></div>
        </div>
        <div v-if="bySide.long" class="card">
          <h3>做多 {{ bySide.long.n }} 笔<span class="badge warn">未验证</span></h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(bySide.long.cum)">{{ pct(bySide.long.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ bySide.long.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(bySide.long.avg) }}</span></div>
        </div>
      </div>

      <!-- 基准对比:三年单边熊市里,什么都不做地持有空单本身就有 +99% 复利。
           不摆出来,上面那个累计收益会被当成策略的本事。 -->
      <div class="card wide compare">
        <h3>
          与「{{ data.compare.benchmark_name }}」比
          <!-- 第二引擎入口(DEC-150):只有配了 seat_follow 的品种(焦煤/玻璃)出现。 -->
          <button v-if="data.seat_follow" class="eng-btn" @click="histEngine = 'second'">
            第二引擎 · 跟{{ data.seat_follow.member.slice(0, 2) }} ▸
          </button>
        </h3>
        <table class="cmp">
          <thead><tr><th></th><th class="num">累计</th><th class="num">夏普</th><th class="num">最大回撤</th></tr></thead>
          <tbody>
            <tr>
              <td>本策略</td>
              <td class="num" :class="pnlClass(data.compare.strategy.cum_pct)">{{ pct(data.compare.strategy.cum_pct) }}</td>
              <td class="num"><b>{{ data.compare.strategy.sharpe }}</b></td>
              <td class="num"><b>{{ pct(data.compare.strategy.max_dd_pct) }}</b></td>
            </tr>
            <tr>
              <td>{{ data.compare.benchmark_name }}</td>
              <td class="num" :class="pnlClass(data.compare.benchmark.cum_pct)">{{ pct(data.compare.benchmark.cum_pct) }}</td>
              <td class="num">{{ data.compare.benchmark.sharpe }}</td>
              <td class="num">{{ pct(data.compare.benchmark.max_dd_pct) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="note">{{ data.compare.note }}</p>
      </div>

      <table class="tbl">
        <thead>
          <tr><th>方向</th><th>进场</th><th>出场</th><th>合约</th><th class="num">进场价</th>
            <th class="num">出场价</th><th class="num">收益</th><th class="num">持有</th><th>出场原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in rows" :key="i">
            <td><span class="side" :class="t.side">{{ sideText(t.side) }}</span></td>
            <td>{{ t.entry_date }}</td>
            <td>{{ t.exit_date ?? '持有中' }}</td>
            <td>{{ t.contract }}<span v-if="t.rolled_from" class="units-badge" :title="`交割纪律出场后自 ${t.rolled_from} 接力(机构无出货信号,DEC-147)`">接力</span></td>
            <td class="num">{{ fmt(t.entry_px) }}<span v-if="(t.units ?? 1) > 1" class="units-badge">×{{ t.units }}批</span></td>
            <td class="num">{{ fmt(t.exit_px) }}</td>
            <td class="num" :class="pnlClass(t.ret_pct)">{{ pct(t.ret_pct) }}</td>
            <td class="num">{{ t.hold_days }} 日</td>
            <td>{{ t.exit_reason ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :page-sizes="[10, 20, 30, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @size-change="changeSize"
        />
      </div>
      <p class="note">
        收益是**毛收益**,未扣手续费与滑点(回测按单边 0.05% 估算,{{ data.stats.trades }} 笔
        合计约 {{ (data.stats.trades * 0.1).toFixed(1) }} 个百分点)。上面那张对比表里的
        策略数字**是扣过成本的**,可以直接和基准比。
      </p>
    </template>

    <!-- ------------------------------------------------ 席位组 -->
    <template v-else-if="tab === 'group'">
      <div class="cards">
        <div class="card wide">
          <h3 v-if="isFixedGroup">固定名单(运营者拍板,不重选)</h3>
          <h3 v-else>换人历史({{ reselectText }}重选一次)</h3>
          <!-- 固定名单(DEC-122):生猪近一年同策略只换席位组回放,固定 5 家回撤 −3.2% 对
               滚动组 −8.2%,运营者取回撤小的;全样本固定名单差得多(+29.5% 对 +117%),
               这一点要在页面上说,别让人以为固定名单是验证出来更优的。 -->
          <p v-if="isFixedGroup" class="note">
            {{ (data.group_log[0]?.members ?? []).join('、') }} 固定,自 {{ data.group_log[0]?.date }} 起生效
            (运营者拍板,生猪 DEC-122 / 焦煤 DEC-125),括号里是拍板时点各家的择时收益(亿元),只作参考不参与选人。
            选人准则对比无一在所有窗口占优(REPORT_LH_SEAT_PICK_v1);**固定名单是按今天的认知挑的,
            全样本回放都弱于滚动择时组**(生猪 +29.5% 对 +117%;焦煤 +114% 对 +244%,近一年 +42% 对 +68%),
            回放数字见「策略方案」页的回测说明;磨底年过去要回头重验。
          </p>
          <!-- 只列**换人**那几次。阵容没变的年份不写一条,不加这句会被读成
               「席位三年没更新」(运营者 2026-08-19 就是这么问的)。 -->
          <p v-if="!isFixedGroup && data.reselect?.last" class="reselect-note">
            最近一次重选 <b>{{ data.reselect.last }}</b>
            <template v-if="data.reselect.changed_at && data.reselect.changed_at < data.reselect.last">
              · <b>阵容未变</b>(上次换人是 {{ data.reselect.changed_at }})
            </template>
            <template v-if="data.reselect.next">,下次 {{ data.reselect.next }}</template>
          </p>
          <p v-if="!isFixedGroup" class="note">
            括号里是该家截至重选时点的**择时收益**(亿元)——把「一直挂着同样大小的仓
            不动」能赚到的钱扣掉之后剩下的部分。按它选人,而不是按谁赚得多:
            实测按总盈亏选样本外 t=3.57、按仓位规模选 t=0.11,按择时收益选 t=5.22。
          </p>
          <div v-for="g in [...data.group_log].reverse()" :key="g.date" class="glog">
            <b>{{ g.date }}</b>
            <!-- 手动换人(DEC-129):不是重选算出来的,要标出来;只管到下一次重选切点。 -->
            <span v-if="g.manual" class="badge warn">手动换人
              <template v-if="g.replace">({{ Object.entries(g.replace).map(([a, b]) => `${a}→${b}`).join('、') }})</template>
              · 到下次重选为止</span>
            <span v-for="m in g.members" :key="m" class="chip">
              {{ m }}<i>{{ g.alpha[m] == null ? '—' : g.alpha[m]!.toFixed(2) }}</i>
            </span>
          </div>
        </div>
      </div>
    </template>

    <!-- ------------------------------------------------ 策略方案 -->
    <template v-else>
      <div class="cards">
        <div class="card wide">
          <h3>怎么算的</h3>
          <ol class="rules">
            <li v-if="isFixedGroup"><b>选人</b>:固定名单({{ (data.group_log[0]?.members ?? []).join('、') }},
              运营者拍板,生猪 DEC-122 / 焦煤 DEC-125),不滚动重选。</li>
            <li v-else><b>选人</b>:{{ reselectText }},按截至当时的**择时收益**排序取前
              {{ data.rules.group_k }} 家。只用当时之前的数据,不看未来。</li>
            <li><b>信号</b>:这 5 家在**全品种合约上的合计净持仓**,取 {{ data.signal.win }} 日变化,
              再用滚动标准差无量纲化得到强度 z。<br>
              <span class="hint">为什么用品种合计而不是逐合约:实测 84.6% 的交易日里同日不同合约
              的持仓变化方向相反——那是移仓换月,逐合约会把一次调仓读成两个相反的信号。</span></li>
            <template v-if="isCampaign">
              <li><b>进场(逐合约战役,DEC-133)</b>:对每届主力合约、每个方向独立判 ——
                **阵营逢跌/亏着加仓的区间累计净加 ≥{{ fmt(Number(data.campaign?.params.confirm)) }} 手确认**,
                且**当日结算不劣于该区间的批次成本**(多:价≤成本;空:价≥成本),
                且**该方向通过聪明钱份额资格**(该向在本品种全部历史合约上的累计战役盈亏
                ≥ 对侧的 {{ ((Number(data.campaign?.params.share) || 0) * 100).toFixed(0) }}% ——
                生猪多头人格 +1.4 亿对空头 +35.7 亿,被它挡在门外:那一侧是套保/接盘,不是聪明钱)。
                次日开盘成交;一个区间只进一笔;**多仓并行**,逐合约各是各的流。<br>
                <span class="hint">左侧进场:跟着机构「亏着还在买」的那批筹码进,位置与他们最好的筹码相同。
                回测 20 日内最大有利偏移中位 +8.1%、最大不利中位 −5.1% —— 先扛后赚是常态,这是左侧的定义。</span></li>
              <li><b>出场</b>:**阵营|净持仓|跌破自进场峰值的 70%(机构卸仓 30%)→ 次日开盘走**;
                或交割纪律(窗口止点前 {{ data.rules.exit_before_delivery ?? 10 }} 个交易日强平)。
                **没有价格止损** —— 回撤控制靠「机构出货第一脚就走」:机构卸仓要卸几个星期、
                全程是他们自己的卖压,小资金一脚就能出干净,没理由替他们扛剩下 70% 的出货。<br>
                <span class="hint">{{ exitText }}</span></li>
              <li><b>成交与计价</b>:信号日收盘判、次日开盘成交(席位排名收盘后才公布);
                计价用**该合约自己的**开盘/结算 —— 左侧建仓常发生在它还不是主力的时候。</li>
              <li><b>回测与丑话</b>:51 笔,简单加总 +118.8pp、逐笔复利 +200.5%、逐年全正、
                最差单笔 −4.1%,超过组内最赚钱席位(东吴 +95.9%/复利 +122.7%)。
                **安慰剂检验 p=0.159**:做空方向本身贡献大头,进场择时的增量在 51 笔样本上
                尚不显著 —— 这是 2026-08-24 上线时如实记下的边界(REPORT_DIP_COST_v1 第五轮)。</li>
            </template>
            <li v-if="!isCampaign && isCost"><b>进场(成本信号,DEC-112)</b>:**机构在场 +
              价格不劣于机构成本(多:价≤成本;空:价≥成本)+ 机构本轮卸仓 ≤30%<template
              v-if="data.rules.cost_need_adding"> + 机构近 {{ data.signal.win }} 日仍同向加仓</template><template
              v-if="data.rules.cost_min_age"> + 机构本轮已持仓 ≥{{ data.rules.cost_min_age }} 日</template>**,
              全部同时成立就进,**不等聪明钱大规模爆发** —— 那是趋势已启动的确认时刻,
              实测那段收益拿不到(REPORT_LIMIT_ENTRY_v1)。换成本信号后进场时机构
              建仓轮龄中位 4 个交易日(流量信号 26 日),进场成本优势中位 +0.32%。
              五道闸门 5/5,见 REPORT_COST_GATES_v1。<br>
              <span class="hint">机构成本是前 20 席位数据上的重建值(加仓日按结算价
              加权),不是交易所真值。散户那一路仍在 —— 只管出场。</span></li>
            <li v-if="!isCampaign && !isCost"><b>进场(方案 C)</b>:**散户反向信号与聪明钱流向同号(共振)** 且
              散户反向 z ≤ −{{ data.signal.enter }} 时做空。两路都要求已过预热期
              ——聪明钱那路的标准化需要 60 个交易日,没预热完不参与判断。
              <template v-if="!data.institution.long_enabled">
                <b>本品种做多支路已关闭</b>——生猪回测里多头 15 笔逐笔累计 −1.5%、
                均值 −0.02%(抛硬币),关掉后夏普 1.96 → 2.39。散户减多时平仓观望,
                不反手做多。<br>
                <span class="hint">玻璃、纯碱**不关**:它们跨了完整周期、做多支路有真实
                机会(FG 双向夏普 0.65 vs 只做空 0.36)。同一套规则换个品种要重新验,
                不能照抄。</span>
              </template>
              <template v-else-if="data.rules.long_mode === 'unload_bounce'">
                **做多腿只由「机构席位组净空、且本轮已卸掉 ≥{{ Math.round(Number(data.rules.long_unload_min ?? 0.5) * 100) }}%」触发**
                (DEC-118):博机构减空之后那一周的反弹(实测 5 日 +1.5%,20 日归零),
                用来联动生猪向上套利;流量 z ≥ {{ data.signal.enter }} 的做多仍然不做。
                知情破例,不是验证通过:做多腿 14 笔均值 +1.06%,整体夏普 2.34 → 2.14、
                回撤 −4.2% → −8.4%。
                <template v-if="data.rules.long_since">
                  <br><b>做多腿只从 {{ data.rules.long_since }} 起开</b>(DEC-124,运营者按磨底年判断):
                  之前的年份单边熊市,只做空;2026 年以来这条腿尚未触发过。
                </template>
              </template>
              <template v-else>z ≥ {{ data.signal.enter }} 时做多(本品种双向)。</template>
            </li>
            <li v-if="!isCampaign && isInstExit"><b>出场(机构出场,DEC-117)</b>:**机构席位组方向翻转,
              或本轮已卸掉 >{{ Math.round(Number(data.rules.cost_unload_max ?? 0.3) * 100) }}%** 就走;
              硬止损 {{ (data.rules.stop * 100).toFixed(0) }}%<template v-if="data.rules.exit_before_delivery">
              / 主力进入交割窗口前 {{ data.rules.exit_before_delivery }} 个交易日强制平仓</template>保留;
              **不看散户翻向、不设持满**。它赢的方式是「机构一松手立刻走、再上手立刻跟」
              (均持有 4 日、笔数比四件套多一半),不是拿得更久;只在焦煤验过,其余品种不用。<br>
              <span class="hint">{{ exitText }}</span></li>
            <li v-else-if="!isCampaign"><b>出场</b>:**散户反向信号翻向** / 硬止损
              {{ (data.rules.stop * 100).toFixed(0) }}% / 持满
              {{ data.rules.max_hold }} 个交易日<template v-if="data.rules.exit_before_delivery">
              / **主力进入交割窗口前 {{ data.rules.exit_before_delivery }} 个交易日
              强制平仓**(散户纪律,这段时间也不进场)</template>。出场只看散户那一路,不要求共振
              ——否则聪明钱一转向就把仓位锁死在里面。<br>
              <span class="hint">{{ exitText }}</span></li>
            <li v-if="!isCampaign && data.rules.rearm_after_delivery !== false"><b>强平后不续仓</b>(DEC-131):临近交割被强制平仓后,
              **同方向信号没断过就不在新主力续仓**——那不是新信号,是同一个状态还挂着;信号消失过至少一天再出现才算新信号,照进。
              反方向不受限。</li>
            <li v-if="!isCampaign"><b>成交</b>:信号日收盘出信号,**次日开盘成交**。席位持仓排名是收盘后
              才公布的(大商所约 15:30-16:00、郑商所约 16:26),按信号日结算价成交
              做不到。这条口径下的收益比原来低很多——**收益几乎全部集中在信号后
              第一天,而那一天拿不到**。</li>
            <li v-if="!isCampaign"><b>计价</b>:主力合约,**换月日用新合约自己的前一日开盘/结算价**。生猪各
              合约相对主力偏离最大 49%,跨合约相除得到的是价差不是收益。</li>
            <li v-if="!isCampaign"><b>为什么选方案 C</b>:同一时间轴(2023-08 起)三个候选——
              原聪明钱单信号 21 笔 +66.7%/胜率 61.9%/回撤 −12.0%/夏普 1.79;
              散户反向单独 21 笔 +73.8%/57.1%/−8.0%/1.74;
              **方案 C 18 笔 +79.8%/61.1%/回撤 −6.8%/夏普 2.23**。
              (这三个数是 DEC-096「持仓跟合约」之前算的,留作当时的选型依据;
              方案 C 在新口径下是 18 笔 +85.0%/66.7%/−5.0%/2.25。)
              <br><span class="hint">**但三者单笔均值差的 t 只有 0.22~0.49,
              统计上分不出高下。**选 C 是运营者的判断(回撤最小、夏普最高),
              不是数据证明它最优——真失效了会知道,但要几个月。</span></li>
            <li v-if="!isCampaign"><b>散户三家怎么来的</b>:三家长期站多头、长期亏钱的席位
              (东方财富、平安期货、徽商期货),把它们的持仓变化反过来用。
              名单**跨品种固定、不逐品种重选**——这是它相对「找聪明钱」的好处:
              没有挑人的过拟合。<br>
              <span class="hint">同一时间轴(2023-08 起、各用各的信号)实测:主信号 21 笔
              净 +66.7%/胜率 61.9%/回撤 −12.0%;散户反向 21 笔 +73.8%/57.1%/−8.0%;
              共振进场 18 笔 +79.8%/61.1%/−6.8%。**单笔均值差的 t 只有 0.13~0.49,
              21 笔样本上分不出高下**——所以不替换主信号。它的用处是当第二意见:
              两者一致还是背离,比它自己的方向更有信息量。共振时回撤小一半,
              方向一致但尚不显著,继续观察。</span></li>
          </ol>
        </div>
        <div class="card wide">
          <h3>必须知道的边界</h3>
          <ul class="caveats">
            <!-- 引擎的文案用 **加粗** 记法,原来 v-text 把星号原样印了出来。 -->
            <li v-for="(c, i) in data.caveats" :key="i" v-html="mdBold(c)" />
          </ul>
        </div>
      </div>
    </template>
  </template>
</template>

<style scoped>
.sub { color: var(--tv-text-secondary); margin: 0 0 22px; }
.loading { padding: 60px; text-align: center; color: var(--tv-text-secondary); }
.err { padding: 20px; background: var(--tv-up-bg); border: 1px solid var(--tv-up);
  border-radius: 6px; color: var(--tv-up); }
.red { color: var(--tv-up); } .green { color: var(--tv-down); } .gray { color: var(--tv-text-muted); }

.symbol-strip {
  position: sticky; top: 0; z-index: 10;
  background: var(--tv-bg-card); border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius); padding: 10px 16px; margin-bottom: 14px;
  box-shadow: var(--tv-shadow);
}
.strip-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.strip-name { font-size: 15px; font-weight: 600; color: var(--tv-text); white-space: nowrap; }
.strip-price { font-size: 24px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums; }
.strip-pct { font-size: 14px; font-variant-numeric: tabular-nums; }
.strip-pill {
  background: var(--tv-warn-bg); color: var(--tv-warn); font-size: 12px;
  padding: 3px 10px; border-radius: var(--tv-radius-sm); white-space: nowrap;
}
/* 未验证的做多持仓要看得出不一样,不能和空头持仓长同一个样 */
.strip-pill.unverified { background: var(--tv-up-bg); color: var(--tv-up); }
.strip-metrics { display: flex; gap: 16px; margin-left: auto; flex-wrap: wrap; }
.strip-metrics .m { display: flex; flex-direction: column; gap: 2px; }
.strip-metrics i { font-style: normal; font-size: 11px; color: var(--tv-text-muted); }
.strip-metrics b { font-size: 13px; font-variant-numeric: tabular-nums; }

.tabbar { display: flex; margin-bottom: 16px; border-bottom: 2px solid var(--tv-border); }
.tab { padding: 9px 22px; cursor: pointer; color: var(--tv-text-secondary);
  border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab.on { color: var(--tv-blue); border-color: var(--tv-blue); font-weight: 600; }

.cards { display: flex; gap: 16px; margin-bottom: 22px; flex-wrap: wrap; }
.card { background: var(--tv-bg-card); border: 1px solid var(--tv-border);
  border-radius: 6px; padding: 18px 20px; flex: 1; min-width: 290px; }
.card.wide { flex-basis: 100%; }
.card h3 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--tv-text-muted); }
.dot.red { background: var(--tv-up); } .dot.green { background: var(--tv-down); }
.big { font-size: 30px; font-weight: 700; margin: 6px 0 12px; font-variant-numeric: tabular-nums; }
/* 「已卸掉」这一行只作展示,不进判据 —— 理由见类型定义上的注释。 */
.unload-warn {
  margin: 2px 0 6px; padding: 5px 7px; font-size: 12px; line-height: 1.5;
  color: var(--tv-text-muted); background: var(--tv-fill-muted, #f6f8fa);
  border-radius: 4px;
}
.peak { font-style: normal; margin-left: 6px; color: var(--tv-text-muted); font-size: 12px; }
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; gap: 10px; }
.kv .k { color: var(--tv-text-secondary); white-space: nowrap; }
.kv .v { text-align: right; font-variant-numeric: tabular-nums; }
.reselect-note { margin: 0 0 8px; font-size: 12px; color: var(--tv-text-secondary); }
/* 信号过期:比风险条更硬 —— 风险条说「这条曲线不好拿」,这条说「这些数字别信」。 */
.stale-banner {
  margin: 0 0 12px;
  padding: 10px 14px;
  border: 1px solid var(--tv-warn, #d98e00);
  border-left-width: 4px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--tv-warn, #d98e00) 14%, transparent);
  font-size: 13px;
  line-height: 1.7;
}
/* 风险条:警示橙描边 + 浅底。不用红绿——那是涨跌语义,借过来会被读成方向。 */
.risk-banner {
  margin: 0 0 14px;
  padding: 12px 14px;
  border: 1px solid var(--tv-warn, #d98e00);
  border-left-width: 4px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--tv-warn, #d98e00) 8%, transparent);
}
.risk-head { font-weight: 700; font-size: 14px; margin-bottom: 6px; }
.risk-banner ul { margin: 0; padding-left: 18px; }
.risk-banner li { font-size: 13px; line-height: 1.75; }
.risk-foot { margin-top: 6px; font-size: 12px; color: var(--tv-text-secondary); }
/* 交割倒计时:撞线是纪律不是行情,用警示色不用涨跌红绿。 */
.kv .v.near { color: var(--tv-warn, #d98e00); }
.kv .v.must { color: var(--tv-warn, #d98e00); font-weight: 700; }
.note { font-size: 12px; color: var(--tv-text-muted); margin: 10px 0 0; line-height: 1.6; }
.hint { font-size: 12px; color: var(--tv-text-muted); }
/* 成本挨在手数后面,弱一档:它是补充信息,手数与增减才是这张卡的主角 */
.cost { color: var(--tv-text-muted); margin-left: 8px; font-size: 12px; }

.meter { margin-bottom: 10px; }
.meter-bar { height: 8px; background: var(--tv-border); border-radius: 4px; overflow: hidden; }
.meter-fill { height: 100%; border-radius: 4px; }
.meter-fill.red { background: var(--tv-up); } .meter-fill.green { background: var(--tv-down); }
.meter-label { display: flex; justify-content: space-between; font-size: 12px;
  color: var(--tv-text-secondary); margin-top: 6px; }

.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500; margin-left: auto; }
.badge.ok { background: var(--tv-down-bg, rgba(8,153,129,.1)); color: var(--tv-down); }
.badge.warn { background: var(--tv-warn-bg); color: var(--tv-warn); }

.caveat-box {
  background: var(--tv-warn-bg); border: 1px solid var(--tv-warn);
  border-radius: 6px; padding: 14px 18px; font-size: 13px; line-height: 1.7;
  color: var(--tv-text); margin-bottom: 22px;
}
.caveat-box b { color: var(--tv-warn); }
/* 机构真转多是**好消息类**的提示,和「信号不可信」那种警告要分得开 */
.caveat-box.flip { background: var(--tv-up-bg); border-color: var(--tv-up); }
.caveat-box.flip b { color: var(--tv-up); }
/* 换月反弹(DEC-123):未触发时灰底低调,触发时与「转多」同色系 */
.caveat-box.roll { background: var(--tv-bg-secondary, #f7f8fa); border-color: var(--tv-border); }
.caveat-box.roll b { color: var(--tv-text-secondary); }
.caveat-box.roll.on { background: var(--tv-up-bg); border-color: var(--tv-up); }
.caveat-box.roll.on b { color: var(--tv-up); }
.roll-hist { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.roll-hist .chip { font-size: 12px; }
.roll-hist .chip i { font-style: normal; margin-left: 4px; }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 8px 10px; border-bottom: 1px solid var(--tv-border); text-align: left; }
.tbl th { color: var(--tv-text-secondary); font-weight: 500; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.side { font-size: 12px; padding: 2px 8px; border-radius: 4px; }
.side.short { background: var(--tv-down-bg, rgba(8,153,129,.1)); color: var(--tv-down); }
.side.long { background: var(--tv-up-bg); color: var(--tv-up); }

.pager { display: flex; justify-content: flex-end; flex-wrap: wrap; margin-top: 14px; }

.compare { margin-bottom: 18px; }
.cmp { border-collapse: collapse; font-size: 13px; }
.cmp th, .cmp td { padding: 6px 22px 6px 0; text-align: left; }
.cmp th { color: var(--tv-text-secondary); font-weight: 500; }
.cmp .num { text-align: right; font-variant-numeric: tabular-nums; }
.cmp tbody tr + tr td { color: var(--tv-text-secondary); }

.glog { padding: 8px 0; border-bottom: 1px solid var(--tv-border); font-size: 13px; }
.glog b { margin-right: 12px; }
/* 第二引擎切换按钮(DEC-150):挂在对比卡标题右侧,主/第二两套历史互切不重叠。 */
.eng-btn {
  float: right; font-size: 13px; padding: 3px 12px; cursor: pointer;
  border: 1px solid var(--el-color-primary, #409eff); color: var(--el-color-primary, #409eff);
  background: transparent; border-radius: 4px;
}
.eng-btn:hover { background: var(--el-color-primary-light-9, #ecf5ff); }
.chip { display: inline-flex; align-items: baseline; gap: 4px; margin-right: 8px;
  background: var(--tv-bg); border: 1px solid var(--tv-border);
  border-radius: 4px; padding: 2px 8px; font-size: 12px; }
.chip i { font-style: normal; color: var(--tv-text-muted); font-variant-numeric: tabular-nums; }

.rules { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; }
.rules li { margin-bottom: 6px; }
.caveats { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; color: var(--tv-text-secondary); }

/* 逐合约战役卡(DEC-133):持仓表与观察表之间的间隔 */
.watch-head {
  margin-top: 16px;
}

/* 合约小窗(DEC-134;DEC-151 改版):一行两窗(运营者 2026-08-28 验收时追加:
   「一行两窗,不然界面太长了」),窄屏收一列。旧的 3 列网格随 DEC-151 作废。 */
.panel-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}
@media (max-width: 1100px) {
  .panel-row { grid-template-columns: minmax(0, 1fr); }
}
.panel-card {
  min-width: 0;
  /* 焦煤价是四位数(@1585),一行两窗时会顶宽 —— 裁切会把成本切成 "@158"
     (运营者 2026-08-28 实拍)。改为需要时横向滚动,不许裁掉数字。 */
  overflow-x: auto;
}
.panel-card h3 {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
/* 数值行允许换行:净持仓/变化/成本挤不下就折到下一行,不许溢出压到隔壁卡 */
.panel-card .kv .v {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  column-gap: 6px;
  min-width: 0;
}
.panel-days {
  font-size: 12px;
  font-weight: normal;
  color: var(--el-text-color-secondary, #909399);
}
.panel-days.near {
  color: #e6a23c;
}
/* DEC-151:窗头沉淀资金(右对齐)与 vs 对照表 */
.panel-sink {
  margin-left: auto;
  font-size: 13px;
  font-weight: normal;
  color: var(--el-text-color-regular, #606266);
}
.panel-vs {
  width: 100%;
  border-collapse: collapse;
  /* 12.5px:焦煤/生猪是四~五位数价(@1585、11860),13.5px 时一行两窗放不下,
     会触发兜底滚动条(运营者 2026-08-28 质疑"其他窗口也这样呢")。压到 12.5 后
     常规宽度下各品种都能整行放下,滚动条只在极窄屏出现。 */
  font-size: 12.5px;
}
.panel-vs td {
  padding: 3px 3px;
  border-bottom: 1px solid var(--el-border-color-extra-light, #f5f7fa);
  white-space: nowrap;
}
.panel-vs .k { color: var(--el-text-color-regular, #606266); width: 52px; }
.panel-vs .num { text-align: right; }
.panel-vs .vs-cell {
  color: var(--el-text-color-placeholder, #c0c4cc);
  text-align: center;
  width: 28px;
  font-size: 12px;
  border-left: 1px dashed var(--el-border-color-lighter, #ebeef5);
  border-right: 1px dashed var(--el-border-color-lighter, #ebeef5);
}
@media (max-width: 720px) {
  .panel-vs { font-size: 12.5px; }
  .panel-vs td { padding: 4px 3px; }
  .panel-vs .k { width: 52px; }
}
/* 筹码地图(DEC-152) */
.chip-map {
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px dashed var(--el-border-color-lighter, #ebeef5);
  font-size: 12.5px;
  color: var(--el-text-color-regular, #606266);
}
.chip-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 5px;
  line-height: 1.9;
}
.chip-line.gray { color: var(--el-text-color-secondary, #909399); }
.chip-side {
  display: inline-block;
  min-width: 20px;
  text-align: center;
  border-radius: 3px;
  font-size: 12px;
  padding: 0 4px;
}
.chip-side.short { background: #e8f7ef; color: #18a058; }
.chip-side.long { background: #fdecec; color: #d03050; }
.chip-map .sep { color: var(--el-text-color-placeholder, #c0c4cc); }
.chip-map .anchor i { font-style: normal; color: var(--el-text-color-secondary, #909399); }
.chip-map .edge-ok { color: #18a058; }
.chip-map .edge-no { color: var(--el-text-color-placeholder, #c0c4cc); }
.panel-card .cost {
  margin-left: 3px;
  font-size: 11px;
  color: var(--el-text-color-secondary, #909399);
}
.panel-note {
  margin-top: 4px;
}

/* 跟批加仓(DEC-135):批次标记与逐批明细 */
.units-badge {
  margin-left: 4px;
  font-size: 11px;
  color: var(--tv-text-muted);
}
.units-detail {
  font-size: 11px;
  color: var(--tv-text-muted);
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
