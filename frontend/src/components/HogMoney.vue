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
import { getSeatNetPosition, type MemberLeg as SeatCost } from '../api'

/**
 * 一个品种一份 JSON,由引擎按品种各写各的(失败也各自隔离)。
 * 组件只负责渲染,规则差异全在 payload 里——**不要在这里按品种写 if**:
 * 生猪只做空、玻璃纯碱双向,这类差异是引擎实测定的,前端硬编码一份迟早对不上。
 */
const props = defineProps<{ instrument: 'LH' | 'FG' | 'SA' }>()
const FILES: Record<string, string> = {
  LH: 'hog_signals.json', FG: 'fg_signals.json', SA: 'sa_signals.json'
}

interface MemberLeg {
  member: string
  net: number
  change: number | null
  on_board: boolean
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
  }
  /**
   * 散户反向维度(DEC-085)。这三家在多个品种上长期站多头、长期亏钱,所以反向取用。
   * **当前只作展示,不参与进出场**——样本还不足以推翻已上线的规则。
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
  group_log: Array<{ date: string; members: string[]; alpha: Record<string, number> }>
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
           max_hold: number; sig_win: number; long_enabled: boolean } & Record<string, unknown>
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
const page = ref(1)
// 与金银历史信号页同一套翻页控件(运营者 2026-08-19:后续品种也都用这个)。
// 每页条数可改,所以不是常量。
const pageSize = ref(20)

/**
 * 组内各家在**当前主力合约**上的持仓成本。
 *
 * 不在引擎里重算,直接走净持仓页那条接口(`seats/net-position`)——那套成本引擎
 * 是 Rust 侧算的、有测试盯着,再用 Python 抄一遍就是同一个事实两处维护。
 * 取不到不影响主页面:成本是锦上添花,信号才是主体。
 */
const costs = ref<Record<string, SeatCost>>({})

onMounted(async () => {
  try {
    // 与金银同一条路:引擎写静态 JSON,nginx 直接服务。带时间戳绕开缓存。
    const res = await fetch(`/smart-money/${FILES[props.instrument]}?t=${Date.now()}`)
    if (!res.ok) throw new Error(`读取失败 ${res.status}`)
    data.value = await res.json()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取信号数据失败'
    return
  }
  const p = data.value
  if (!p?.members.length || !p.contract) return
  try {
    const { data: net } = await getSeatNetPosition({
      instrument: props.instrument,
      members: p.members.map((m) => m.member),
      contract: p.contract
    })
    costs.value = Object.fromEntries(net.latest_members.map((m) => [m.member, m]))
  } catch {
    // 成本取不到就不显示这一列,页面照常用
  }
})

/**
 * 某家在当前主力合约上的净持仓成本。
 *
 * 按它自己那条腿取:净空看空单成本、净多看多单成本。覆盖不全时标出覆盖手数
 * ——那说明有合约的成本不可知(建仓当日无结算价、或数据起点之前就持有),
 * 不能让人以为这个均价覆盖了全部持仓。
 */
function memberCost(name: string): string {
  const c = costs.value[name]
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
const reselectText = computed(() => {
  const m = data.value?.rules.reselect_months ?? 0
  return m === 12 ? '每年' : m === 1 ? '每月' : `每 ${m} 个月`
})
/** 「N 笔全部由 反向/止损 触发」——原因和笔数都是数出来的。 */
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

/** 信号强度条:z 相对进场门槛的比例,超过门槛就满格。 */
const zRatio = computed(() => {
  const z = data.value?.signal.z
  const e = data.value?.signal.enter ?? 1
  if (z === null || z === undefined) return 0
  return Math.min(Math.abs(z) / e, 1)
})

const rows = computed(() => {
  const h = data.value?.history ?? []
  // 新的在前:历史表看的是最近发生了什么
  const sorted = [...h].reverse()
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
          <div class="kv"><span class="k">现价</span><span class="v">{{ fmt(data.price) }} · {{ data.contract }}</span></div>
          <template v-if="data.position">
            <div class="kv"><span class="k">进场</span>
              <span class="v">{{ data.position.entry_date }} @ {{ fmt(data.position.entry_px) }}
                <template v-if="rolled">· {{ data.position.contract }}</template>
              </span></div>
            <div class="kv"><span class="k">方向</span><span class="v">{{ sideText(data.position.side) }}</span></div>
            <div class="kv"><span class="k">持有</span><span class="v">{{ data.position.hold_days }} 个交易日</span></div>
          </template>
          <p v-if="rolled" class="note">
            这笔持仓跨过了主力换月:进场在 {{ data.position!.contract }},现价是
            {{ data.contract }} 的。**两个价格不在同一个合约上,不要相减**——
            收益按逐日累积算(换月日用新合约自己的前一日结算价),生猪各合约价差
            最大到 49%,相减会得出一个完全错误的数。
          </p>
          <div v-else-if="!data.position" class="kv">
            <span class="k">进场条件</span>
            <span class="v">信号强度需达 {{ data.signal.enter }}(现 {{ fmt(data.signal.z, 2) }})</span>
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
              <span>{{ (data.signal.z ?? 0) < 0 ? '机构在加空' : '机构在减空/加多' }}</span>
              <b>{{ fmt(data.signal.z, 2) }}</b>
            </div>
          </div>
          <div class="kv"><span class="k">合计净持仓</span><span class="v">{{ fmt(data.signal.net) }} 手</span></div>
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
                <span v-if="m.change !== null" :class="pnlClass(m.change)">
                  ({{ m.change >= 0 ? '+' : '' }}{{ fmt(m.change) }})
                </span>
                <span class="cost">成本 {{ memberCost(m.member) }}</span>
              </template>
              <span v-else class="gray">当日未上榜</span>
            </span>
          </div>
          <p class="note">
            席位组{{ reselectText }}按历史择时收益重选一次,不是固定名单。
            成本是**{{ data.contract }} 这一个合约**上的净持仓成本(推算),按结算价推
            ——不是成交均价,我们看不到成交明细。
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
              <span>{{ (data.retail.z ?? 0) > 0 ? '散户在减多 → 反向看涨' : '散户在加多 → 反向看跌' }}</span>
              <b>{{ fmt(data.retail.z, 2) }}</b>
            </div>
          </div>
          <div class="kv"><span class="k">三家合计净持仓</span>
            <span class="v" :class="(data.retail.net ?? 0) > 0 ? 'red' : 'green'">
              {{ fmt(data.retail.net) }} 手</span></div>
          <div class="kv"><span class="k">{{ data.signal.win }} 日变化</span>
            <span class="v" :class="pnlClass(-(data.retail.change ?? 0))">{{ fmt(data.retail.change) }} 手</span></div>
          <div v-for="m in data.retail.members" :key="m.member" class="kv">
            <span class="k">　{{ m.member }}</span>
            <span class="v">
              <template v-if="m.on_board">{{ fmt(m.net) }} 手
                <span v-if="m.change !== null" :class="pnlClass(-m.change)">
                  ({{ m.change >= 0 ? '+' : '' }}{{ fmt(m.change) }})</span>
              </template>
              <span v-else class="gray">当日未上榜</span>
            </span>
          </div>
          <p class="note">{{ data.retail.note }}</p>
        </div>
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
        <h3>与「{{ data.compare.benchmark_name }}」比</h3>
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
            <td>{{ t.contract }}</td>
            <td class="num">{{ fmt(t.entry_px) }}</td>
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
          <h3>重选历史({{ reselectText }}一次)</h3>
          <p class="note">
            括号里是该家截至重选时点的**择时收益**(亿元)——把「一直挂着同样大小的仓
            不动」能赚到的钱扣掉之后剩下的部分。按它选人,而不是按谁赚得多:
            实测按总盈亏选样本外 t=3.57、按仓位规模选 t=0.11,按择时收益选 t=5.22。
          </p>
          <div v-for="g in [...data.group_log].reverse()" :key="g.date" class="glog">
            <b>{{ g.date }}</b>
            <span v-for="m in g.members" :key="m" class="chip">
              {{ m }}<i>{{ g.alpha[m]?.toFixed(2) ?? '—' }}</i>
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
            <li><b>选人</b>:{{ reselectText }},按截至当时的**择时收益**排序取前
              {{ data.rules.group_k }} 家。只用当时之前的数据,不看未来。</li>
            <li><b>信号</b>:这 5 家在**全品种合约上的合计净持仓**,取 {{ data.signal.win }} 日变化,
              再用滚动标准差无量纲化得到强度 z。<br>
              <span class="hint">为什么用品种合计而不是逐合约:实测 84.6% 的交易日里同日不同合约
              的持仓变化方向相反——那是移仓换月,逐合约会把一次调仓读成两个相反的信号。</span></li>
            <li><b>进场(方案 C)</b>:**散户反向信号与聪明钱流向同号(共振)** 且
              散户反向 z ≤ −{{ data.signal.enter }} 时做空。两路都要求已过预热期
              ——聪明钱那路的标准化需要 60 个交易日,没预热完不参与判断。
              <template v-if="!data.institution.long_enabled">
                <b>本品种做多支路已关闭</b>——生猪回测里多头 15 笔逐笔累计 −1.5%、
                均值 −0.02%(抛硬币),关掉后夏普 1.96 → 2.39。散户减多时平仓观望,
                不反手做多。<br>
                <span class="hint">玻璃、纯碱**不关**:它们跨了完整周期、做多支路有真实
                机会(FG 双向夏普 1.63 vs 只做空 1.04)。同一套规则换个品种要重新验,
                不能照抄。</span>
              </template>
              <template v-else>z ≥ {{ data.signal.enter }} 时做多(本品种双向)。</template>
            </li>
            <li><b>出场</b>:**散户反向信号翻向** / 硬止损
              {{ (data.rules.stop * 100).toFixed(0) }}% / 持满
              {{ data.rules.max_hold }} 个交易日。出场只看散户那一路,不要求共振
              ——否则聪明钱一转向就把仓位锁死在里面。<br>
              <span class="hint">{{ exitText }}</span></li>
            <li><b>计价</b>:主力合约,**换月日用新合约自己的前一日结算价**。生猪各合约相对
              主力偏离最大 49%,跨合约相除得到的是价差不是收益。</li>
            <li><b>为什么选方案 C</b>:同一时间轴(2023-08 起)三个候选——
              原聪明钱单信号 21 笔 +79.7%/胜率 71.4%/回撤 −9.4%/夏普 2.39;
              散户反向单独 21 笔 +91.4%/76.2%/−6.8%/2.42;
              **方案 C 18 笔 +88.4%/72.2%/回撤 −4.1%/夏普 2.80**。
              <br><span class="hint">**但三者单笔均值差的 t 只有 0.22~0.49,
              统计上分不出高下。**选 C 是运营者的判断(回撤最小、夏普最高),
              不是数据证明它最优——真失效了会知道,但要几个月。</span></li>
            <li><b>散户三家怎么来的</b>:三家长期站多头、长期亏钱的席位
              (东方财富、平安期货、徽商期货),把它们的持仓变化反过来用。
              名单**跨品种固定、不逐品种重选**——这是它相对「找聪明钱」的好处:
              没有挑人的过拟合。<br>
              <span class="hint">同一时间轴(2023-08 起、各用各的信号)实测:主信号 21 笔
              净 +79.7%/胜率 71.4%/回撤 −9.4%;散户反向 21 笔 +91.4%/76.2%/−6.8%;
              共振进场 18 笔 +88.4%/72.2%/−4.1%。**单笔均值差的 t 只有 0.22~0.49,
              21 笔样本上分不出高下**——所以不替换主信号。它的用处是当第二意见:
              两者一致还是背离,比它自己的方向更有信息量。共振时回撤小一半,
              方向一致但尚不显著,继续观察。</span></li>
          </ol>
        </div>
        <div class="card wide">
          <h3>必须知道的边界</h3>
          <ul class="caveats">
            <li v-for="(c, i) in data.caveats" :key="i" v-text="c" />
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
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; gap: 10px; }
.kv .k { color: var(--tv-text-secondary); white-space: nowrap; }
.kv .v { text-align: right; font-variant-numeric: tabular-nums; }
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
.chip { display: inline-flex; align-items: baseline; gap: 4px; margin-right: 8px;
  background: var(--tv-bg); border: 1px solid var(--tv-border);
  border-radius: 4px; padding: 2px 8px; font-size: 12px; }
.chip i { font-style: normal; color: var(--tv-text-muted); font-variant-numeric: tabular-nums; }

.rules { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; }
.rules li { margin-bottom: 6px; }
.caveats { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; color: var(--tv-text-secondary); }
</style>
