<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

// 机构资金信号页。数据由信号引擎每日盘后生成(nginx 静态服务的
// /smart-money/signals.json),本页只负责在平台内渲染,不做任何计算。
interface Condition {
  value: number
  target: number
  pass: boolean
}
interface Position {
  entry_date: string
  entry_px: number
  inst_cost: number | null
  pnl_pct: number
  stop_px: number
  fade_days: number
  fade_target: number
  hold_days: number
}
interface MarketState {
  instrument: string
  name: string
  state: string
  last_close: number
  main_contract: string
  /** 现价在近 60 日高低区间的位置,1=贴着高点。只标注不判定。旧 JSON 无此字段。 */
  range_pos?: number | null
  /** 现价在近 250 日收盘里的分位。旧 JSON 无此字段。 */
  pct_250d?: number | null
  /** 距本轮高点的回撤深度(伦敦金美元口径,±10%分轮)。旧 JSON 无此字段。 */
  dd_round?: number | null
  /** 本轮高点参照价(美元/盎司)。 */
  round_high?: number | null
  /** 重挫态(粘滞):本轮已回撤逾15%,直到新轮开启。仅黄金。 */
  crash_zone?: boolean
  /** 重挫共振(仅黄金):回撤≥15% 且共振,次日开盘市价买入。 */
  crash_ready?: boolean
  conditions: { score: Condition; dist_low: Condition; netq: Condition; range_pos?: Condition }
  all_pass: boolean
  prospective_zone: [number, number] | null
  prospective_cost: number | null
  position: Position | null
  weights: Record<string, number>
  weights_raw?: Record<string, number>
  theta: number
  env_block?: string
  env_boost?: string
}
interface SignalsPayload {
  generated_at: string
  data_date: string
  markets: Record<string, MarketState>
  ratio: {
    value: number
    zone: string
    note: string
    source?: string
    percentile: number
    mean: number
  }
  alerts: { type: string; level: string; market: string; date: string; text: string }[]
  activity: {
    date: string
    member: string
    market: string
    market_name: string
    action: string
    strength: number
    weight: number
    hands: number
  }[]
  history: {
    market: string
    name: string
    signal_date: string
    /** 信号日的区间位置,同上。旧 JSON 无此字段。 */
    range_pos?: number | null
    seats: { member: string; strength: number }[]
    score: number
    zone: [number, number] | null
    inst_cost: number | null
    entry_date: string | null
    entry_px: number | null
    exit_date: string | null
    exit_px: number | null
    result: string
    relay: boolean
    /** 重挫共振单(仅黄金):市价执行,免起点条件。旧 JSON 无此字段。 */
    crash?: boolean
    ret_pct: number | null
    marks: { cross_resonance: boolean; spread_legs: string[]; goldman_combo: boolean }
  }[]
  alert_history: {
    type: string
    label: string
    market: string
    start: string
    end: string
    note: string
  }[]
  stats: Record<string, { count: number; win_rate: number; avg: number; total: number; since: string }>
  rules: { group: string[]; buy: string; sell: string; cond_seats: string[] }
}

const data = ref<SignalsPayload | null>(null)
const error = ref('')
const tab = ref<'today' | 'history' | 'weights' | 'rules'>('today')

// 权重显示:撞上限时补一个未截断的真实 t 值。计算不受影响,见 RULES.weight_clip。
function weightText(mk: { weights: Record<string, number>; weights_raw?: Record<string, number> },
                    member: string): string {
  const w = mk?.weights?.[member]
  if (w === undefined || w === null) return '—'
  const raw = mk?.weights_raw?.[member]
  if (raw !== undefined && raw !== null && raw - w > 0.005) {
    return `${w}(实际 ${raw})`
  }
  return String(w)
}

const alertMeta: Record<string, [string, string]> = {
  buy: ['danger', '买入触发'],
  sell_now: ['danger', '卖出执行'],
  sell_watch: ['warn', '卖出监控'],
  rare_flip: ['info', '稀有翻空'],
  pair_window: ['pair', '配对窗口'],
  flee: ['danger', '主力跑路'],
  high_zone: ['warn', '持仓高位提醒']
}

const latestMarks = computed(() => data.value?.history[0]?.marks ?? null)

// —— 历史信号分页 ——
//
// 引擎现在写全量历史（百来条），一屏铺不下。页码用 1 起，与界面上显示的一致。
const historyPage = ref(1)
const historyPageSize = ref(20)
const historyTotal = computed(() => data.value?.history.length ?? 0)
const historyRows = computed(() => {
  const all = data.value?.history ?? []
  const start = (historyPage.value - 1) * historyPageSize.value
  return all.slice(start, start + historyPageSize.value)
})
/**
 * 改每页条数时回到第一页。
 *
 * 不回的话：停在第 7 页再把每页从 10 改成 50，第 7 页早已超出总页数，表会变成空的，
 * 看上去像数据没了。
 */
function changeHistorySize(size: number) {
  historyPageSize.value = size
  historyPage.value = 1
}

function fmt(v: number | null | undefined, decimals = 0): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}
function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}
function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'gray'
  return v > 0 ? 'red' : v < 0 ? 'green' : 'gray'
}
/**
 * 区间位置的展示。回测结论(2026-08-15):高位单恰是主要利润来源,所以这里
 * 只如实标注位置,不做任何"危险"暗示——上色只为扫一眼能分层。
 */
function posText(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${Math.round(v * 100)}%`
}
function posClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v >= 0.75 ? 'pos-high' : v <= 0.25 ? 'pos-low' : ''
}
function decimalsOf(market: string): number {
  return market === 'AU' ? 2 : 0
}
function costEdge(p: Position): number | null {
  if (!p.inst_cost) return null
  return ((p.inst_cost - p.entry_px) / p.inst_cost) * 100
}
function ratioNeedle(value: number): string {
  return `${Math.max(0, Math.min(100, ((value - 40) / 60) * 100))}%`
}
function renderAlertText(text: string): string {
  return text.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
}
function barWidth(ratio: number): string {
  return `${Math.max(2, Math.min(100, ratio))}%`
}

onMounted(async () => {
  try {
    const response = await fetch(`/smart-money/signals.json?t=${Date.now()}`)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    data.value = await response.json()
  } catch (cause) {
    error.value = `读取信号数据失败:${cause}。请确认信号引擎已运行。`
  }
})
</script>

<template>
  <div class="page smart-money">
    <h1>机构资金</h1>
    <p v-if="data" class="sub">
      跟随金银双强七席位资金动向,每日收盘后自动计算。数据日期 <b>{{ data.data_date }}</b> ·
      计算于 {{ data.generated_at }}
    </p>
    <p v-else class="sub">跟随金银双强七席位资金动向,每日收盘后自动计算。</p>

    <div v-if="error" class="err">{{ error }}</div>
    <div v-else-if="!data" class="loading">正在读取信号数据…</div>

    <template v-else>
      <div class="tabbar">
        <div class="tab" :class="{ on: tab === 'today' }" @click="tab = 'today'">今日信号</div>
        <div class="tab" :class="{ on: tab === 'history' }" @click="tab = 'history'">历史信号</div>
        <div class="tab" :class="{ on: tab === 'weights' }" @click="tab = 'weights'">席位权重</div>
        <div class="tab" :class="{ on: tab === 'rules' }" @click="tab = 'rules'">策略方案</div>
      </div>

      <template v-if="tab === 'today'">
        <div class="cards">
          <div v-for="key in ['AU', 'AG']" :key="key" class="card">
            <template v-if="data.markets[key].position">
              <h3><span class="dot hold" />{{ data.markets[key].name }} — 持有中</h3>
              <div class="big" :class="pnlClass(data.markets[key].position!.pnl_pct)">
                {{ pct(data.markets[key].position!.pnl_pct) }}
              </div>
              <div class="kv">
                <span class="k">买入</span>
                <span class="v">{{ data.markets[key].position!.entry_date }} @
                  {{ fmt(data.markets[key].position!.entry_px, decimalsOf(key)) }}</span>
              </div>
              <div v-if="data.markets[key].position!.inst_cost" class="kv">
                <span class="k">机构加权成本</span>
                <span class="v">
                  {{ fmt(data.markets[key].position!.inst_cost, decimalsOf(key)) }}
                  <span v-if="(costEdge(data.markets[key].position!) ?? 0) > 0" class="green">
                    (我方低 {{ costEdge(data.markets[key].position!)!.toFixed(1) }}%)
                  </span>
                </span>
              </div>
              <div class="kv">
                <span class="k">现价</span>
                <span class="v">{{ fmt(data.markets[key].last_close, decimalsOf(key)) }} ·
                  {{ data.markets[key].main_contract }}</span>
              </div>
              <div v-if="data.markets[key].range_pos !== undefined" class="kv">
                <span class="k">现价位置</span>
                <span class="v" :class="posClass(data.markets[key].range_pos)">
                  60日区间 {{ posText(data.markets[key].range_pos) }}
                  <span class="gray">· 250日分位 {{ posText(data.markets[key].pct_250d) }}</span>
                  <span v-if="data.markets[key].crash_zone" class="pos-high">
                    · 伦敦金本轮回撤已达15%(现 {{ posText(data.markets[key].dd_round) }},重挫态)</span>
                </span>
              </div>
              <div class="kv">
                <span class="k">止损价</span>
                <span class="v red">{{ fmt(data.markets[key].position!.stop_px, decimalsOf(key)) }}</span>
              </div>
              <div class="kv">
                <span class="k">卖出监控</span>
                <span class="v">
                  <b v-if="data.markets[key].position!.fade_days >= data.markets[key].position!.fade_target"
                     class="red">已满足,下一交易日开盘卖出</b>
                  <template v-else>
                    静默 <b>{{ data.markets[key].position!.fade_days }}/{{ data.markets[key].position!.fade_target }}</b> 日
                  </template>
                </span>
              </div>
              <div class="kv">
                <span class="k">持有</span>
                <span class="v">{{ data.markets[key].position!.hold_days }} 个交易日</span>
              </div>
            </template>
            <template v-else>
              <h3>
                <span class="dot" :class="data.markets[key].all_pass ? 'hold' : 'watch'" />
                {{ data.markets[key].name }} — {{ data.markets[key].all_pass ? '买入触发' : '观察中' }}
              </h3>
              <div class="big gray">{{ data.markets[key].all_pass ? '待挂单' : '无持仓' }}</div>
              <div class="kv">
                <span class="k">进场条件</span>
                <span class="v">
                  {{ Object.values(data.markets[key].conditions).filter((c) => c.pass).length }}
                  / {{ Object.keys(data.markets[key].conditions).length }} 满足
                </span>
              </div>
              <div class="kv">
                <span class="k">现价</span>
                <span class="v">{{ fmt(data.markets[key].last_close, decimalsOf(key)) }} ·
                  {{ data.markets[key].main_contract }}</span>
              </div>
              <div v-if="data.markets[key].range_pos !== undefined" class="kv">
                <span class="k">现价位置</span>
                <span class="v" :class="posClass(data.markets[key].range_pos)">
                  60日区间 {{ posText(data.markets[key].range_pos) }}
                  <span class="gray">· 250日分位 {{ posText(data.markets[key].pct_250d) }}</span>
                  <span v-if="data.markets[key].crash_zone" class="pos-high">
                    · 伦敦金本轮回撤已达15%(现 {{ posText(data.markets[key].dd_round) }},重挫态)</span>
                </span>
              </div>
              <div v-if="data.markets[key].prospective_zone" class="kv">
                <span class="k">参考买入上限</span>
                <span class="v">≤ {{ fmt(data.markets[key].prospective_zone![1], decimalsOf(key)) }}
                  <span class="gray">(机构成本 {{ fmt(data.markets[key].prospective_cost, decimalsOf(key)) }})</span>
                </span>
              </div>
              <div v-if="data.markets[key].env_block" class="kv">
                <span class="k">环境</span><span class="v red">{{ data.markets[key].env_block }}</span>
              </div>
              <div v-if="data.markets[key].env_boost" class="kv">
                <span class="k">环境</span><span class="v green">{{ data.markets[key].env_boost }}</span>
              </div>
            </template>
          </div>

          <div class="card">
            <h3><span class="dot ok" />金银比环境</h3>
            <div class="gauge">
              <div class="gwrap">
                <div class="gtrack"><div class="gneedle" :style="{ left: ratioNeedle(data.ratio.value) }" /></div>
                <div class="gscale"><span>&lt;48 银高估</span><span>55~85 正常</span><span>&gt;85 银低估</span></div>
              </div>
              <div class="big" style="margin: 0">{{ data.ratio.value }}</div>
            </div>
            <div class="kv" style="margin-top: 8px">
              <span class="k">状态</span><span class="v"><b>{{ data.ratio.zone }}</b></span>
            </div>
            <div class="kv">
              <span class="k">历史分位</span>
              <span class="v">{{ data.ratio.percentile }}%(均值 {{ data.ratio.mean }})</span>
            </div>
            <div class="footnote">{{ data.ratio.note }}<br />{{ data.ratio.source || '' }}</div>
          </div>
        </div>

        <div class="section">
          <h2>触发提示</h2>
          <div class="desc">收盘后席位数据入库即计算;买卖执行均为次日开盘(T+1)。</div>
          <div v-for="(alert, index) in data.alerts" :key="index"
               class="alert" :class="(alertMeta[alert.type] ?? ['info'])[0]">
            <span class="tag">{{ (alertMeta[alert.type] ?? ['info', '提示'])[1] }}</span>
            <!-- eslint-disable-next-line vue/no-v-html — 文本来自自家引擎,仅 ** 加粗替换 -->
            <div v-html="renderAlertText(alert.text)" />
            <span class="t">{{ alert.date }}</span>
          </div>
          <div v-if="!data.alerts.length" class="gray">当前无触发提示</div>
        </div>

        <div class="section">
          <h2>买点条件</h2>
          <div class="desc">全部满足 → 次日按机构成本区间挂单。门槛按各品种权重每年自动校准。</div>
          <template v-for="key in ['AU', 'AG']" :key="key">
            <div class="cond-title">
              {{ data.markets[key].name }}
              <span class="gray">(门槛 θ={{ data.markets[key].theta }})</span>
            </div>
            <div class="cond">
              <div class="item">
                <div class="name">
                  <span>① 加权增多分数</span>
                  <span :class="data.markets[key].conditions.score.pass ? 'green' : 'gray'">
                    {{ data.markets[key].conditions.score.value }} / {{ data.markets[key].conditions.score.target }}
                    {{ data.markets[key].conditions.score.pass ? '✓' : '' }}
                  </span>
                </div>
                <div class="bar" :class="{ pass: data.markets[key].conditions.score.pass }">
                  <i :style="{ width: barWidth((data.markets[key].conditions.score.value / data.markets[key].conditions.score.target) * 100) }" />
                </div>
              </div>
              <div class="item">
                <div class="name">
                  <span>② 距 60 日低点 &lt;12%</span>
                  <span :class="data.markets[key].conditions.dist_low.pass ? 'green' : 'red'">
                    +{{ data.markets[key].conditions.dist_low.value }}%
                    {{ data.markets[key].conditions.dist_low.pass ? '✓' : '✗' }}
                  </span>
                </div>
                <div class="bar" :class="data.markets[key].conditions.dist_low.pass ? 'pass' : 'warn'">
                  <i :style="{ width: barWidth((data.markets[key].conditions.dist_low.value / 12) * 100) }" />
                </div>
              </div>
              <div class="item">
                <div class="name">
                  <span>③ 七席位净仓 &lt;60 分位</span>
                  <span :class="data.markets[key].conditions.netq.pass ? 'green' : 'red'">
                    {{ data.markets[key].conditions.netq.value }} 分位
                    {{ data.markets[key].conditions.netq.pass ? '✓' : '✗' }}
                  </span>
                </div>
                <div class="bar" :class="data.markets[key].conditions.netq.pass ? 'pass' : 'warn'">
                  <i :style="{ width: barWidth(data.markets[key].conditions.netq.value) }" />
                </div>
              </div>
              <!-- 第四条只约束首进场;中继不受限,由引擎侧保证。旧 JSON 无此字段则整块不显示。 -->
              <div v-if="data.markets[key].conditions.range_pos" class="item">
                <div class="name">
                  <span>④ 位于 60 日区间下 70%(仅首进场)</span>
                  <span :class="data.markets[key].conditions.range_pos!.pass ? 'green' : 'red'">
                    {{ data.markets[key].conditions.range_pos!.value }}%
                    {{ data.markets[key].conditions.range_pos!.pass ? '✓' : '✗' }}
                  </span>
                </div>
                <div class="bar" :class="data.markets[key].conditions.range_pos!.pass ? 'pass' : 'warn'">
                  <i :style="{ width: barWidth(data.markets[key].conditions.range_pos!.value / 0.7) }" />
                </div>
              </div>
            </div>
          </template>
        </div>

        <div class="section">
          <h2>七席位近期动态</h2>
          <div class="desc">最近三周的有效增多事件(权重为当年生效值)。</div>
          <div class="scroll-x">
            <table>
              <thead>
                <tr><th>日期</th><th>席位</th><th>品种</th><th>动作</th><th>强度</th><th>权重</th><th>单日净增</th></tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in data.activity" :key="index">
                  <td>{{ row.date }}</td>
                  <td>{{ row.member }}</td>
                  <td><span class="pill" :class="row.market.toLowerCase()">{{ row.market_name }}</span></td>
                  <td>{{ row.action }}</td>
                  <td>{{ row.strength }}{{ row.strength >= 3 ? '(满)' : '' }}</td>
                  <td>{{ row.weight }}</td>
                  <td class="red">+{{ fmt(row.hands) }} 手</td>
                </tr>
                <tr v-if="!data.activity.length"><td colspan="7" class="gray">近三周无事件</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="section">
          <h2>参考标记</h2>
          <div class="desc">不改变买卖规则,提供人工复核维度(取最近一次信号的标记状态)。</div>
          <span class="badge" :class="{ on: latestMarks?.cross_resonance }"><span class="s" />金银共振</span>
          <span class="badge" :class="{ on: latestMarks?.spread_legs?.length }">
            <span class="s" />比价腿警示<template v-if="latestMarks?.spread_legs?.length">({{ latestMarks.spread_legs.join('、') }})</template>
          </span>
          <span class="badge" :class="{ on: latestMarks?.goldman_combo }"><span class="s" />高盛金银组合(历史命中 91%)</span>
          <!-- 「正常」不点红:红是警示位,正常态点红等于让人天天看假警报。 -->
          <span class="badge" :class="{ on: data.ratio.zone !== '正常' }"><span class="s" />金银比 {{ data.ratio.value }} {{ data.ratio.zone }}</span>
        </div>
      </template>

      <div v-else-if="tab === 'history'" class="section">
        <h2>历史信号</h2>
        <div class="desc">
          共 {{ historyTotal }} 条,2015 年起的全部信号。收益按复权价计算,已扣双边成本。
          <b>下面的成熟期统计是另一个口径</b>——只计 2019 年起<b>已了结</b>的交易,
          不含持有中与未回踩放弃的,所以笔数比上面少:
          <template v-for="(stat, key) in data.stats" :key="key">
            {{ data.markets[key]?.name }} {{ stat.count }} 笔 / 胜率 {{ stat.win_rate }}% / 累计 {{ stat.total }}% ·
          </template>
        </div>
        <div class="scroll-x">
          <table>
            <thead>
              <tr>
                <th>品种</th><th>信号日</th><th>分数</th><th>位置</th><th>触发席位</th><th>买入上限</th>
                <th>机构成本</th><th>进场</th><th>出场</th><th>结果</th><th>收益</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in historyRows" :key="index">
                <td><span class="pill" :class="row.market.toLowerCase()">{{ row.name }}</span></td>
                <td>
                  {{ row.signal_date }}
                  <span v-if="row.crash" class="pill crash" title="重挫共振:伦敦金±10%分轮内自轮高回撤触及15%后的共振,免起点条件,次日开盘市价买入(不挂区间)。仅黄金,历史同类日胜率73%,回放12笔11胜。">重挫</span>
                  <span v-else-if="row.relay" class="pill relay" title="消退/止损离场后席位再共振的再进场,免贴低点与低仓条件,次日开盘市价">中继</span>
                </td>
                <td>{{ row.score }}</td>
                <td :class="posClass(row.range_pos)" :title="row.range_pos === null || row.range_pos === undefined ? '' : '信号日收盘在近60日价格区间的位置,100%=贴着高点。只标注,不参与买卖判定。'">
                  {{ posText(row.range_pos) }}
                </td>
                <td class="wrap">{{ row.seats.map((s) => `${s.member}(${s.strength})`).join('、') || '—' }}</td>
                <td>{{ row.zone ? `≤${fmt(row.zone[1], decimalsOf(row.market))}` : '市价' }}</td>
                <td class="gray">{{ row.inst_cost ? fmt(row.inst_cost, decimalsOf(row.market)) : '—' }}</td>
                <td>
                  <template v-if="row.entry_date">
                    <div class="d2">{{ row.entry_date }}</div>
                    <div class="d2 gray">@ {{ fmt(row.entry_px, decimalsOf(row.market)) }}</div>
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="row.exit_date">
                    <div class="d2">{{ row.exit_date }}</div>
                    <div class="d2 gray">@ {{ fmt(row.exit_px, decimalsOf(row.market)) }}</div>
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <span class="pill" :class="row.result === '持有中' ? 'holding' : (row.ret_pct ?? 0) > 0 ? 'win' : 'loss'">
                    {{ row.result }}
                  </span>
                </td>
                <td :class="pnlClass(row.ret_pct)">{{ row.ret_pct === null ? '—' : pct(row.ret_pct) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="pager">
          <el-pagination
            v-model:current-page="historyPage"
            :page-size="historyPageSize"
            :page-sizes="[10, 20, 30, 50]"
            :total="historyTotal"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @size-change="changeHistorySize"
          />
        </div>

        <h2 class="alert-history-title">警报历史</h2>
        <div class="desc">
          做空侧只有复合结构警报,不是逐笔交易信号(单边跟随机构空单已被数据三次否定,空单主体是产业套保)。
          警报活跃段内:持有的金银多单建议离场,且警报后 40 个交易日内系统自动禁止中继再进场。
        </div>
        <div v-if="data.alert_history?.length" class="scroll-x">
          <table>
            <thead>
              <tr><th>类型</th><th>市场</th><th>起</th><th>止</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="(seg, index) in data.alert_history" :key="index">
                <td><span class="pill alarm">{{ seg.label }}</span></td>
                <td>{{ seg.market }}</td>
                <td>{{ seg.start }}</td>
                <td>{{ seg.end }}</td>
                <td class="wrap gray">{{ seg.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="gray">全历史暂无警报触发段</div>
      </div>

      <div v-else-if="tab === 'weights'" class="section">
        <h2>席位权重(当年生效)</h2>
        <div class="desc">
          权重 = 该席位截至上年末全部增多事件"后 20 日收益"的 t 值,截断 [0,5],样本 &lt;30 记 0。
          撞到上限的席位会同时标出未截断的真实 t 值(如 <b>5.00(实际 6.56)</b>)——
          计算一律用截断后的值,标注只为看清真实差距。
          每年 1 月 1 日自动重算,不手工设定。门槛 θ = 1.2 × 组内最大权重
          (黄金 {{ data.markets.AU.theta }} / 白银 {{ data.markets.AG.theta }})。
        </div>
        <table>
          <thead>
            <tr><th>席位</th><th>黄金权重</th><th>白银权重</th><th>计分条件</th></tr>
          </thead>
          <tbody>
            <tr v-for="member in data.rules.group" :key="member">
              <td>{{ member }}</td>
              <td>{{ weightText(data.markets.AU, member) }}</td>
              <td>{{ weightText(data.markets.AG, member) }}</td>
              <td class="gray">
                {{ data.rules.cond_seats.includes(member) ? '仅贴 60 日低点 <5% 时计分' : '全场景计分' }}
              </td>
            </tr>
          </tbody>
        </table>
        <div class="footnote">
          名单标准:金银两个品种都盈利且都进各自前 30 名、事件预测力 t 值双 ≥1.5(单品种选手已剔除)。
        </div>
      </div>

      <div v-else class="section">
        <h2>策略方案</h2>
        <div class="desc">跟随金银双强席位的持仓变化,不使用任何趋势/突破类价格指标。</div>
        <div class="rule-line"><span class="lab">信号组</span><span>{{ data.rules.group.join(' · ') }}</span></div>
        <div class="rule-line"><span class="lab">买点</span><span>{{ data.rules.buy }}</span></div>
        <div class="rule-line"><span class="lab">卖点</span><span>{{ data.rules.sell }}</span></div>
        <div class="rule-line">
          <span class="lab">事件定义</span>
          <span>净多增加且多头腿主导,且 |ΔNet/全市场持仓| ≥ 该席位近 250 个上榜日的 80 分位(每家自适应门槛)</span>
        </div>
        <div class="rule-line">
          <span class="lab">时序</span>
          <span>15:00 收盘后席位数据入库 → 当晚计算信号 → 次日开盘执行(买卖同)</span>
        </div>
        <div class="rule-line">
          <span class="lab">环境调制</span>
          <span>金银比 &lt;48 白银禁新买点;&gt;85 白银买点建议加倍仓位</span>
        </div>
        <div class="rule-line">
          <span class="lab">已验证无效</span>
          <span class="gray">跟随机构撤退卖出 · 机构止损跟随 · 顶部预测信号 · 趋势突破确认 · 金字塔加仓(V 型市负贡献)</span>
        </div>
        <div class="footnote">完整回测与验证过程见 research/REPORT_AU_v1.md</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.smart-money h1 { font-size: 26px; font-weight: 700; margin: 0 0 6px; }
.sub { color: var(--tv-text-secondary); margin: 0 0 22px; }
.loading { padding: 60px; text-align: center; color: var(--tv-text-secondary); }
.err { padding: 20px; background: var(--tv-up-bg); border: 1px solid var(--tv-up); border-radius: 6px; color: var(--tv-up); }
.red { color: var(--tv-up); } .green { color: var(--tv-down); } .gray { color: var(--tv-text-muted); }
.tabbar { display: flex; margin-bottom: 16px; border-bottom: 2px solid var(--tv-border); }
.tab { padding: 9px 22px; cursor: pointer; color: var(--tv-text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab.on { color: var(--tv-blue); border-color: var(--tv-blue); font-weight: 600; }
.cards { display: flex; gap: 16px; margin-bottom: 22px; flex-wrap: wrap; }
.card { background: var(--tv-bg-card); border: 1px solid var(--tv-border); border-radius: 6px; padding: 18px 20px; flex: 1; min-width: 290px; }
.card h3 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot.hold { background: var(--tv-up); box-shadow: 0 0 6px var(--tv-up); }
.dot.watch { background: var(--tv-text-muted); }
.dot.ok { background: var(--tv-down); }
.big { font-size: 22px; font-weight: 700; margin-bottom: 6px; }
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; gap: 10px; }
.kv .k { color: var(--tv-text-secondary); white-space: nowrap; }
.kv .v { text-align: right; }
.section { background: var(--tv-bg-card); border: 1px solid var(--tv-border); border-radius: 6px; padding: 20px 22px; margin-bottom: 22px; }
/* 分页条贴着表格下沿,窄屏时允许换行——挤成一行会把跳页框推出可视区。 */
.pager { display: flex; justify-content: flex-end; flex-wrap: wrap; margin-top: 14px; }
/* 区间位置分层色:高位橙、低位青,中段不上色。只为扫一眼能分层,无褒贬。 */
.pos-high { color: var(--tv-warn); font-weight: 600; }
.pos-low { color: var(--tv-down); font-weight: 600; }
.pill.crash { background: var(--tv-up-bg); color: var(--tv-up); }
.section h2 { font-size: 16px; margin: 0 0 4px; }
.section .desc { color: var(--tv-text-secondary); font-size: 12.5px; margin-bottom: 14px; }
.alert { display: flex; gap: 12px; padding: 13px 16px; border-radius: 6px; margin-bottom: 10px; align-items: flex-start; }
/* 彩色标签底上的白字两个主题都可读,无对应 token,保留白色字面量。 */
.alert .tag { font-size: 12px; padding: 2px 10px; border-radius: 4px; white-space: nowrap; font-weight: 600; margin-top: 1px; color: #fff; }
.alert.danger { background: var(--tv-up-bg); border: 1px solid var(--tv-up); }
.alert.danger .tag { background: var(--tv-up); }
.alert.warn { background: var(--tv-warn-bg); border: 1px solid var(--tv-warn); }
.alert.warn .tag { background: var(--tv-warn); }
.alert.info { background: var(--tv-bg-inset); border: 1px solid var(--tv-border); }
.alert.info .tag { background: var(--tv-text-secondary); }
.alert.pair { background: var(--tv-blue-bg); border: 1px solid var(--tv-blue); }
.alert.pair .tag { background: var(--tv-blue); }
.alert .t { color: var(--tv-text-secondary); font-size: 12px; margin-left: auto; white-space: nowrap; }
.cond-title { margin: 14px 0 8px; font-weight: 600; }
.cond { display: flex; gap: 26px; flex-wrap: wrap; }
.cond .item { flex: 1; min-width: 230px; }
.cond .name { font-size: 13px; color: var(--tv-text-secondary); margin-bottom: 6px; display: flex; justify-content: space-between; gap: 8px; }
.bar { height: 8px; background: var(--tv-bg-hover); border-radius: 4px; overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 4px; background: var(--tv-text-muted); }
.bar.pass i { background: var(--tv-down); }
.bar.warn i { background: var(--tv-warn); }
.badge { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--tv-border); border-radius: 16px; padding: 5px 14px; font-size: 13px; margin: 0 8px 8px 0; background: var(--tv-bg-inset); }
.badge .s { width: 8px; height: 8px; border-radius: 50%; background: var(--tv-text-muted); }
.badge.on { background: var(--tv-up-bg); border-color: var(--tv-up); }
.badge.on .s { background: var(--tv-up); }
.scroll-x { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--tv-text-secondary); font-weight: 500; padding: 8px 7px; border-bottom: 1px solid var(--tv-border); background: var(--tv-bg-inset); white-space: nowrap; }
td { padding: 8px 7px; border-bottom: 1px solid var(--tv-border); white-space: nowrap; }
td.wrap { white-space: normal; min-width: 150px; }
/* 进场/出场一格两行:日期上、价格下。并排写要 170px,叠起来 90px 就够——
   正是这两列把整张表顶出屏幕,逼出横向滚动条。 */
.d2 { line-height: 1.35; }
.pill { font-size: 12px; padding: 1px 8px; border-radius: 4px; background: var(--tv-bg-inset); color: var(--tv-text-secondary); }
.pill.au { background: var(--tv-warn-bg); color: var(--tv-warn); }
.pill.ag { background: var(--tv-bg-inset); color: var(--tv-text-secondary); }
/* 红涨绿跌:止盈红、止损/亏损绿(全站统一,与收益列的 red/green 同一套语义)。 */
.pill.win { background: var(--tv-up-bg); color: var(--tv-up); }
.pill.loss { background: var(--tv-down-bg); color: var(--tv-down); }
.pill.holding { background: var(--tv-blue-bg); color: var(--tv-blue); }
.pill.relay { background: var(--tv-warn-bg); color: var(--tv-warn); margin-left: 4px; cursor: help; }
/* 警报历史的类型标签:警示语义,红系(与盈亏 pill 区分开)。 */
.pill.alarm { background: var(--tv-up-bg); color: var(--tv-up); }
.alert-history-title { margin-top: 28px; }
.gauge { display: flex; align-items: center; gap: 14px; }
.gwrap { flex: 1; }
.gtrack { height: 10px; border-radius: 5px; position: relative;
  background: linear-gradient(90deg, var(--tv-up) 0 14%, var(--tv-warn) 14% 24%, var(--tv-down) 24% 62%, var(--tv-warn) 62% 78%, var(--tv-up) 78% 100%); }
.gneedle { position: absolute; top: -5px; width: 2.5px; height: 20px; background: var(--tv-text); border-radius: 2px; }
.gscale { display: flex; justify-content: space-between; color: var(--tv-text-secondary); font-size: 11px; margin-top: 4px; }
.footnote { color: var(--tv-text-muted); font-size: 12px; margin-top: 6px; }
.rule-line { padding: 7px 0; border-bottom: 1px dashed var(--tv-border); display: flex; gap: 14px; }
.rule-line .lab { color: var(--tv-text-secondary); width: 80px; flex-shrink: 0; }
</style>
