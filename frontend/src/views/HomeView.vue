<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getDataHealth, getSeatNetPosition, getSpreadMonitor, type DataHealthResponse, type SpreadMonitorItem } from '../api'
// 排序与到齐判定在 overview.ts 里,那边有测试:这两处错了都不会露馅——
// 排序错了照样列出三个组合、到齐判定错了有缺口的日子会显示成全绿。
import { dayCompleteness, rankByExtremity } from '../overview'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import GoldSilverReport from '../components/GoldSilverReport.vue'

// 总览页。回答进站第一眼想知道的四件事：今天有没有该看的套利机会、持仓要不要
// 动、数据到齐没有、日更有没有出问题。这四件事以前要点进三个页面才凑得齐。
//
// 本页不做任何计算，只把各处已有的结论摆到一起——套利位置由每日快照算好，
// 机构资金信号由引擎算好，数据到齐与否是库里的事实。多算一遍就是多一个会跟
// 上游对不上的地方。

const router = useRouter()
const auth = useAuthStore()
const health = useHealthStore()

const monitor = ref<SpreadMonitorItem[]>([])
const monitorDate = ref<string | null>(null)
const dataHealth = ref<DataHealthResponse | null>(null)
const signals = ref<SmartMoneySnapshot | null>(null)
const loading = ref(true)
const failed = ref<string[]>([])

interface SmartMoneyPosition {
  entry_date: string
  entry_px: number
  pnl_pct: number
  stop_px: number
  fade_days: number
  fade_target: number
  hold_days: number
}
interface SmartMoneyMarket {
  instrument: string
  name: string
  state: string
  last_close: number
  position: SmartMoneyPosition | null
}
interface SmartMoneySnapshot {
  data_date: string
  markets: Record<string, SmartMoneyMarket>
  alerts: { level: string; market: string; text: string }[]
}

onMounted(async () => {
  // 四路各自成败，一路挂掉不该让整页空白——首页最怕的就是「什么都不显示，
  // 也不说为什么」。谁没取到就在页尾如实点名。
  const results = await Promise.allSettled([
    getSpreadMonitor(),
    getDataHealth(),
    fetch(`/smart-money/signals.json?t=${Date.now()}`).then((r) => {
      if (!r.ok) throw new Error(String(r.status))
      return r.json() as Promise<SmartMoneySnapshot>
    }),
    health.refresh(),
    auth.refresh()
  ])
  let availableDates: string[] = []
  if (results[0].status === 'fulfilled') {
    monitor.value = results[0].value.data.items
    monitorDate.value = results[0].value.data.as_of
    availableDates = results[0].value.data.available_dates
  } else failed.value.push('套利监控')
  if (results[1].status === 'fulfilled') dataHealth.value = results[1].value.data
  else failed.value.push('数据到齐情况')
  if (results[2].status === 'fulfilled') signals.value = results[2].value
  else failed.value.push('机构资金信号')
  loading.value = false
  // 迷你走势图在主内容之后异步补画,取不到就不画,不算加载失败。
  void loadPriceSparks()
  void loadMonitorBars(availableDates)
})

// —— 卡片迷你走势图(2026-08-16 运营者拍板)——
//
// 持仓卡:进场日以来的合成收盘价,衬在浮盈数字下。价格线用净持仓接口的
// 合成价(持仓量加权),形状参考用——进场价/现价仍以引擎口径为准。
// 套利卡:近 10 个交易日的触发组合数(默认阈值现算,与大数字同口径)。
interface Spark {
  line: string
  area: string
  entryY: number
}
const sparks = ref<Record<string, Spark>>({})
const monitorBars = ref<{ date: string; count: number }[]>([])

function buildSpark(values: number[], entry: number): Spark | null {
  if (values.length < 2) return null
  const low = Math.min(...values, entry)
  const high = Math.max(...values, entry)
  const span = high - low || 1
  const x = (i: number) => (i / (values.length - 1)) * 100
  const y = (v: number) => 30 - ((v - low) / span) * 28
  const points = values.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`)
  return {
    line: `M${points.join(' L')}`,
    area: `M${points.join(' L')} L100,32 L0,32 Z`,
    entryY: y(entry)
  }
}

async function loadPriceSparks() {
  const markets = signals.value?.markets
  if (!markets) return
  for (const market of Object.values(markets)) {
    const position = market.position
    if (!position) continue
    try {
      // 任选一家常年在榜的席位:接口按品种回全套日线,席位只影响净持仓列。
      const { data } = await getSeatNetPosition({ instrument: market.instrument, members: ['中信期货'] })
      const closes = data.days
        .filter((day) => day.trade_date >= position.entry_date && day.close_price !== null)
        .map((day) => Number(day.close_price))
      const spark = buildSpark(closes, position.entry_px)
      if (spark) sparks.value = { ...sparks.value, [market.instrument]: spark }
    } catch { /* 画不出就不画 */ }
  }
}

async function loadMonitorBars(availableDates: string[]) {
  const dates = [...availableDates].sort().slice(-10)
  if (!dates.length) return
  try {
    const snapshots = await Promise.all(dates.map((date) => getSpreadMonitor(undefined, date)))
    monitorBars.value = snapshots.map((snapshot, index) => ({
      date: dates[index],
      count: snapshot.data.items.filter((item) => item.alert !== null).length
    }))
  } catch { /* 同上 */ }
}
const monitorBarMax = computed(() => Math.max(1, ...monitorBars.value.map((bar) => bar.count)))

// —— 套利监控 ——
const triggered = computed(() => monitor.value.filter((item) => item.alert !== null))
const topTriggered = computed(() => rankByExtremity(monitor.value, 3))

function comboLabel(item: SpreadMonitorItem) {
  return item.is_cross_variety
    ? `${item.contract_1} − ${item.contract_2}`
    : `${item.instrument_1} ${item.contract_1.slice(-4)}-${item.contract_2.slice(-4)}`
}
function positionLabel(item: SpreadMonitorItem) {
  const years = item.years?.position
  const pair = item.pair.position
  const useYears = item.years?.alert !== null && item.years?.alert !== undefined
  const value = useYears ? years : pair
  if (value === null || value === undefined) return '—'
  const scope = useYears ? '历年' : '当年'
  const side = Number(value) >= 0.5 ? '高位' : '低位'
  return `${Number(value).toFixed(2)} ${scope}${side}`
}

// —— 机构资金 ——
/** 引擎状态是英文枚举,卡片上要说中文。名单外的值原样显示,不猜。 */
const STATE_LABELS: Record<string, string> = {
  holding: '持有中',
  watching: '观望中',
  armed: '待触发'
}
const marketCards = computed(() => {
  const markets = signals.value?.markets
  if (!markets) return []
  return Object.values(markets).map((market) => ({
    instrument: market.instrument,
    name: market.name,
    state: market.state,
    position: market.position,
    alert: signals.value?.alerts.find((a) => a.market === market.instrument) ?? null
  }))
})

// —— 数据到齐 ——
const expected = computed(() => dataHealth.value?.expected_exchanges ?? [])
/** 交易所代码 → 中文名。名单外的代码原样显示，不猜。 */
const EXCHANGE_NAMES: Record<string, string> = {
  SHFE: '上期所',
  DCE: '大商所',
  CZCE: '郑商所',
  CFFEX: '中金所',
  GFEX: '广期所'
}
const exchangeRows = computed(() =>
  expected.value.map((code) => {
    const seat = dataHealth.value?.seats.find((day) => day.exchanges.includes(code))
    const price = dataHealth.value?.prices.find((day) => day.exchanges.includes(code))
    return {
      code,
      name: EXCHANGE_NAMES[code] ?? code,
      seatDate: seat?.trade_date ?? null,
      priceDate: price?.trade_date ?? null,
      // 最新一日该所数据的首次入库时刻——运营者要的"采集源几点更新"画像。
      // 注意 2026-08-16 之前入库的日子,这个时刻反映的是最后一轮补采。
      seatAt: seat?.arrivals?.[code] ?? null
    }
  })
)
/** 最近 10 个交易日，每天席位与行情是否都覆盖了全部交易所。 */
const recentDays = computed(() =>
  dayCompleteness(dataHealth.value?.seats ?? [], dataHealth.value?.prices ?? [], expected.value.length)
)
const latestComplete = computed(() => recentDays.value[0]?.complete ?? false)
const missingDays = computed(() => recentDays.value.filter((day) => !day.complete).length)
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <h1>总览</h1>
      <p v-if="monitorDate">数据截至 {{ monitorDate }}</p>
    </div>

    <el-skeleton v-if="loading" :rows="6" animated />

    <template v-else>
      <div class="overview-grid">
        <el-card shadow="never" class="ov-card">
          <template #header>
            <div class="ov-head">
              <span>套利监控</span>
              <el-tag v-if="triggered.length" type="danger" size="small" effect="light">
                {{ triggered.length }} 组触发
              </el-tag>
              <el-tag v-else type="success" size="small" effect="light">无触发</el-tag>
            </div>
          </template>
          <div class="ov-big">
            {{ triggered.length }}<span class="ov-unit">/ {{ monitor.length }} 组</span>
          </div>
          <p class="ov-sub">位置贴近历史极值的组合数</p>
          <div class="ov-rows">
            <div v-for="row in topTriggered" :key="comboLabel(row)" class="ov-row">
              <span class="k">{{ comboLabel(row) }}</span>
              <span class="v">{{ positionLabel(row) }}</span>
            </div>
            <p v-if="!topTriggered.length" class="ov-empty">今天没有组合贴近极值。</p>
          </div>
          <div v-if="monitorBars.length" class="ov-spark">
            <svg viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
              <rect
                v-for="(bar, index) in monitorBars"
                :key="bar.date"
                :x="index * (100 / monitorBars.length) + 1"
                :width="100 / monitorBars.length - 2"
                :y="32 - (bar.count / monitorBarMax) * 28 - 2"
                :height="(bar.count / monitorBarMax) * 28 + 2"
                :class="index === monitorBars.length - 1 ? 'spark-bar-now' : 'spark-bar'"
              />
            </svg>
            <div class="ov-spark-foot">
              <span>近 {{ monitorBars.length }} 交易日触发数</span>
              <span>今 {{ monitorBars[monitorBars.length - 1].count }}</span>
            </div>
          </div>
          <el-button text type="primary" size="small" @click="router.push('/spread-analytics/monitor')">
            看全部 →
          </el-button>
        </el-card>

        <el-card v-for="market in marketCards" :key="market.instrument" shadow="never" class="ov-card">
          <template #header>
            <div class="ov-head">
              <span>机构资金 · {{ market.name }}</span>
              <el-tag :type="market.position ? 'warning' : 'info'" size="small" effect="light">
                {{ market.position ? '持有中' : (STATE_LABELS[market.state] ?? market.state) }}
              </el-tag>
            </div>
          </template>
          <template v-if="market.position">
            <div class="ov-big">
              {{ market.position.fade_days }}<span class="ov-unit">/ {{ market.position.fade_target }} 日无增多</span>
            </div>
            <p class="ov-sub">满 {{ market.position.fade_target }} 日则次日开盘卖出</p>
            <div class="ov-rows">
              <div class="ov-row">
                <span class="k">硬止损</span><span class="v">{{ market.position.stop_px }}</span>
              </div>
              <div class="ov-row">
                <span class="k">浮动盈亏</span>
                <span class="v" :class="market.position.pnl_pct >= 0 ? 'up' : 'down'">
                  {{ market.position.pnl_pct >= 0 ? '+' : '' }}{{ market.position.pnl_pct.toFixed(2) }}%
                </span>
              </div>
              <div class="ov-row">
                <span class="k">已持有</span><span class="v">{{ market.position.hold_days }} 日</span>
              </div>
            </div>
            <div v-if="sparks[market.instrument]" class="ov-spark">
              <svg viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
                <path :d="sparks[market.instrument].area" class="spark-area" />
                <path :d="sparks[market.instrument].line" class="spark-line" />
                <line x1="0" x2="100" :y1="sparks[market.instrument].entryY" :y2="sparks[market.instrument].entryY" class="spark-entry" />
              </svg>
              <div class="ov-spark-foot">
                <span>{{ market.position.entry_date }} 进场</span>
                <span>持仓期合成价</span>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="ov-big ov-idle">空仓</div>
            <p class="ov-sub">等待七席位共振进场信号</p>
          </template>
          <el-alert
            v-if="market.alert"
            :title="market.alert.text"
            :type="market.alert.level === 'warn' ? 'warning' : 'error'"
            :closable="false"
            show-icon
            class="ov-alert"
          />
        </el-card>

        <el-card shadow="never" class="ov-card ov-span2">
          <template #header>
            <div class="ov-head">
              <span>数据到齐了吗</span>
              <el-tag :type="latestComplete ? 'success' : 'warning'" size="small" effect="light">
                {{ latestComplete ? `${expected.length} 所齐` : '有缺口' }}
              </el-tag>
            </div>
          </template>
          <div class="ov-rows">
            <div v-for="row in exchangeRows" :key="row.code" class="ov-row">
              <span class="k">{{ row.name }} {{ row.code }} · 席位 / 行情</span>
              <span class="v">
                {{ row.seatDate ?? '—' }}
                <template v-if="row.priceDate !== row.seatDate"> / {{ row.priceDate ?? '—' }}</template>
                <span v-if="row.seatAt" class="ov-arrival">{{ row.seatAt }} 到达</span>
              </span>
            </div>
            <p v-if="!exchangeRows.length" class="ov-empty">回看窗内没有任何数据。</p>
          </div>
        </el-card>

        <el-card shadow="never" class="ov-card">
          <template #header>
            <div class="ov-head">
              <span>近 {{ recentDays.length }} 个交易日</span>
              <el-tag :type="missingDays ? 'warning' : 'success'" size="small" effect="light">
                {{ missingDays ? `${missingDays} 天有缺` : '全到齐' }}
              </el-tag>
            </div>
          </template>
          <div class="ov-strip">
            <span
              v-for="day in [...recentDays].reverse()"
              :key="day.trade_date"
              class="ov-tick"
              :class="{ miss: !day.complete }"
              :title="`${day.trade_date} ${day.complete ? '已到齐' : '有缺口'}`"
            />
          </div>
          <p class="ov-sub ov-pipeline">
            采集 → 投影 → 三禾修正 → 品种汇总 → 掉榜反推 → 套利快照
          </p>
          <div class="ov-rows">
            <div class="ov-row">
              <span class="k">最新交易日</span>
              <span class="v">{{ recentDays[0]?.trade_date ?? '—' }}</span>
            </div>
          </div>
        </el-card>
      </div>

      <!-- 报告表自己取数、自己管加载失败，不并进上面那四路 allSettled——
           它比首页那几张卡片重得多，挂了也不该让总览一起空白。 -->
      <GoldSilverReport />

      <div class="ov-foot">
        <span>版本 <code>{{ health.version?.git_sha?.slice(0, 7) ?? 'local' }}</code></span>
        <span>API {{ health.ready?.status ?? 'unknown' }}</span>
        <span v-if="auth.workspace">Workspace {{ auth.workspace.name }}</span>
      </div>

      <el-alert
        v-if="failed.length"
        :title="`这些没能加载：${failed.join('、')}`"
        type="warning"
        show-icon
        :closable="false"
        class="ov-alert"
      />
    </template>
  </section>
</template>

<style scoped>
.overview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
@media (max-width: 1100px) {
  .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .overview-grid { grid-template-columns: minmax(0, 1fr); }
}
.ov-span2 { grid-column: span 2; }
@media (max-width: 720px) {
  .ov-span2 { grid-column: auto; }
}
.ov-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}
.ov-big {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.ov-idle {
  color: var(--el-text-color-secondary);
  font-size: 22px;
}
.ov-unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 5px;
}
.ov-sub {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
  margin: 4px 0 0;
}
.ov-pipeline { margin-top: 10px; line-height: 1.5; }
.ov-rows { margin-top: 10px; }
.ov-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 12.5px;
}
.ov-row:last-child { border-bottom: 0; }
.ov-row .k { color: var(--el-text-color-secondary); }
.ov-row .v {
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-primary);
  white-space: nowrap;
}
/* 国内看盘惯例：红涨绿跌。 */
.ov-row .v.up { color: var(--tv-up); }
.ov-row .v.down { color: var(--tv-down); }
.ov-empty {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
  margin: 6px 0 0;
}
.ov-arrival {
  margin-left: 8px;
  font-size: 11px;
  color: var(--tv-text-muted);
  font-variant-numeric: tabular-nums;
}
.ov-spark {
  margin-top: 10px;
}
.ov-spark svg {
  display: block;
  width: 100%;
  height: 44px;
}
.spark-line {
  fill: none;
  stroke: var(--tv-up);
  stroke-width: 1.4;
  vector-effect: non-scaling-stroke;
}
.spark-area {
  fill: var(--tv-up-bg);
  stroke: none;
}
.spark-entry {
  stroke: var(--tv-border-strong);
  stroke-width: 1;
  stroke-dasharray: 3 3;
  vector-effect: non-scaling-stroke;
}
.spark-bar {
  fill: var(--tv-border-strong);
}
.spark-bar-now {
  fill: var(--tv-blue);
}
.ov-spark-foot {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--tv-text-muted);
  margin-top: 3px;
}
.ov-strip {
  display: flex;
  gap: 5px;
  margin-top: 4px;
}
.ov-tick {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--el-color-success);
}
.ov-tick.miss { background: var(--el-border-color); }
.ov-alert { margin-top: 12px; }
.ov-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-top: 18px;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.ov-foot code {
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
  font-size: 11.5px;
}
</style>
