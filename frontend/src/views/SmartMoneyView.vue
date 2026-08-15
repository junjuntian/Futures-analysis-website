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
  conditions: { score: Condition; dist_low: Condition; netq: Condition }
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
  flee: ['danger', '主力跑路']
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
                <span class="k">三条件</span>
                <span class="v">
                  {{ Object.values(data.markets[key].conditions).filter((c) => c.pass).length }} / 3 满足
                </span>
              </div>
              <div class="kv">
                <span class="k">现价</span>
                <span class="v">{{ fmt(data.markets[key].last_close, decimalsOf(key)) }} ·
                  {{ data.markets[key].main_contract }}</span>
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
          <h2>买点三条件</h2>
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
          <span class="badge on"><span class="s" />金银比 {{ data.ratio.value }} {{ data.ratio.zone }}</span>
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
                <th>品种</th><th>信号日</th><th>分数</th><th>触发席位</th><th>买入上限</th>
                <th>机构成本</th><th>进场</th><th>出场</th><th>结果</th><th>收益</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in historyRows" :key="index">
                <td><span class="pill" :class="row.market.toLowerCase()">{{ row.name }}</span></td>
                <td>
                  {{ row.signal_date }}
                  <span v-if="row.relay" class="pill relay" title="消退/止损离场后席位再共振的再进场,免贴低点与低仓条件,次日开盘市价">中继</span>
                </td>
                <td>{{ row.score }}</td>
                <td class="wrap">{{ row.seats.map((s) => `${s.member}(${s.strength})`).join('、') || '—' }}</td>
                <td>{{ row.zone ? `≤${fmt(row.zone[1], decimalsOf(row.market))}` : '市价' }}</td>
                <td class="gray">{{ row.inst_cost ? fmt(row.inst_cost, decimalsOf(row.market)) : '—' }}</td>
                <td>{{ row.entry_date ? `${row.entry_date} @ ${fmt(row.entry_px, decimalsOf(row.market))}` : '—' }}</td>
                <td>{{ row.exit_date ? `${row.exit_date} @ ${fmt(row.exit_px, decimalsOf(row.market))}` : '—' }}</td>
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
                <td><span class="pill loss">{{ seg.label }}</span></td>
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
.sub { color: #909399; margin: 0 0 22px; }
.loading { padding: 60px; text-align: center; color: #909399; }
.err { padding: 20px; background: #fef0f0; border: 1px solid #fbc4c4; border-radius: 6px; color: #f56c6c; }
.red { color: #f56c6c; } .green { color: #67c23a; } .gray { color: #909399; }
.tabbar { display: flex; margin-bottom: 16px; border-bottom: 2px solid #ebeef5; }
.tab { padding: 9px 22px; cursor: pointer; color: #606266; border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab.on { color: #409eff; border-color: #409eff; font-weight: 600; }
.cards { display: flex; gap: 16px; margin-bottom: 22px; flex-wrap: wrap; }
.card { background: #fff; border: 1px solid #e4e7ed; border-radius: 6px; padding: 18px 20px; flex: 1; min-width: 290px; }
.card h3 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot.hold { background: #f56c6c; box-shadow: 0 0 6px #f56c6c88; }
.dot.watch { background: #c0c4cc; }
.dot.ok { background: #67c23a; }
.big { font-size: 22px; font-weight: 700; margin-bottom: 6px; }
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; gap: 10px; }
.kv .k { color: #909399; white-space: nowrap; }
.kv .v { text-align: right; }
.section { background: #fff; border: 1px solid #e4e7ed; border-radius: 6px; padding: 20px 22px; margin-bottom: 22px; }
/* 分页条贴着表格下沿,窄屏时允许换行——挤成一行会把跳页框推出可视区。 */
.pager { display: flex; justify-content: flex-end; flex-wrap: wrap; margin-top: 14px; }
.section h2 { font-size: 16px; margin: 0 0 4px; }
.section .desc { color: #909399; font-size: 12.5px; margin-bottom: 14px; }
.alert { display: flex; gap: 12px; padding: 13px 16px; border-radius: 6px; margin-bottom: 10px; align-items: flex-start; }
.alert .tag { font-size: 12px; padding: 2px 10px; border-radius: 4px; white-space: nowrap; font-weight: 600; margin-top: 1px; }
.alert.danger { background: #fef0f0; border: 1px solid #fbc4c4; }
.alert.danger .tag { background: #f56c6c; color: #fff; }
.alert.warn { background: #fdf6ec; border: 1px solid #f5dab1; }
.alert.warn .tag { background: #e6a23c; color: #fff; }
.alert.info { background: #f4f4f5; border: 1px solid #e4e7ed; }
.alert.info .tag { background: #909399; color: #fff; }
.alert.pair { background: #ecf5ff; border: 1px solid #b3d8ff; }
.alert.pair .tag { background: #409eff; color: #fff; }
.alert .t { color: #909399; font-size: 12px; margin-left: auto; white-space: nowrap; }
.cond-title { margin: 14px 0 8px; font-weight: 600; }
.cond { display: flex; gap: 26px; flex-wrap: wrap; }
.cond .item { flex: 1; min-width: 230px; }
.cond .name { font-size: 13px; color: #606266; margin-bottom: 6px; display: flex; justify-content: space-between; gap: 8px; }
.bar { height: 8px; background: #ebeef5; border-radius: 4px; overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 4px; background: #c0c4cc; }
.bar.pass i { background: #67c23a; }
.bar.warn i { background: #e6a23c; }
.badge { display: inline-flex; align-items: center; gap: 6px; border: 1px solid #e4e7ed; border-radius: 16px; padding: 5px 14px; font-size: 13px; margin: 0 8px 8px 0; background: #fafafa; }
.badge .s { width: 8px; height: 8px; border-radius: 50%; background: #c0c4cc; }
.badge.on { background: #fef0f0; border-color: #fbc4c4; }
.badge.on .s { background: #f56c6c; }
.scroll-x { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #909399; font-weight: 500; padding: 9px 10px; border-bottom: 1px solid #ebeef5; background: #fafafa; white-space: nowrap; }
td { padding: 9px 10px; border-bottom: 1px solid #f2f6fc; white-space: nowrap; }
td.wrap { white-space: normal; min-width: 220px; }
.pill { font-size: 12px; padding: 1px 8px; border-radius: 4px; background: #f4f4f5; color: #606266; }
.pill.au { background: #fdf6ec; color: #b88230; }
.pill.ag { background: #f4f4f5; color: #606266; }
.pill.win { background: #f0f9eb; color: #67c23a; }
.pill.loss { background: #fef0f0; color: #f56c6c; }
.pill.holding { background: #ecf5ff; color: #409eff; }
.pill.relay { background: #fdf2e9; color: #e6a23c; margin-left: 4px; cursor: help; }
.alert-history-title { margin-top: 28px; }
.gauge { display: flex; align-items: center; gap: 14px; }
.gwrap { flex: 1; }
.gtrack { height: 10px; border-radius: 5px; position: relative;
  background: linear-gradient(90deg, #f56c6c 0 14%, #e6a23c 14% 24%, #67c23a 24% 62%, #e6a23c 62% 78%, #f56c6c 78% 100%); }
.gneedle { position: absolute; top: -5px; width: 2.5px; height: 20px; background: #303133; border-radius: 2px; }
.gscale { display: flex; justify-content: space-between; color: #909399; font-size: 11px; margin-top: 4px; }
.footnote { color: #c0c4cc; font-size: 12px; margin-top: 6px; }
.rule-line { padding: 7px 0; border-bottom: 1px dashed #ebeef5; display: flex; gap: 14px; }
.rule-line .lab { color: #909399; width: 80px; flex-shrink: 0; }
</style>
