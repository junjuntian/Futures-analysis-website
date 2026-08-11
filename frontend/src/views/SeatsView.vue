<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { CandlestickSeriesOption, EChartsOption } from 'echarts'
import {
  getSeatBuilding,
  getSeatPositions,
  getSpreadVarieties,
  type BuildingDay,
  type SeatPositionRow
} from '../api'
import SpreadChart from '../components/SpreadChart.vue'

// 席位与日期由两个子页共用：先选好一次，切标签不用重选。
const member = ref('')
const tradeDate = ref('')
const members = ref<string[]>([])
const availableDates = ref<string[]>([])
const tab = ref<'positions' | 'building'>('positions')

// 席位持仓
const rows = ref<SeatPositionRow[]>([])
const instrumentFilter = ref<string[]>([])
const loadingPositions = ref(false)

// 建仓过程
const buildingInstrument = ref('')
const buildingContract = ref('')
const days = ref<BuildingDay[]>([])
const multiplier = ref<string | null>(null)
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
    // 第一次进来没选会员：默认选名录里的第一个，而不是让人对着空白发呆。
    if (!member.value && data.members.length) {
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

const num = (value: string | null) => (value === null || value === '' ? null : Number(value))

/** 按品种分组，每个品种下按合约列出多空持仓——三禾那张表的形状。 */
interface ContractLine {
  contract: string
  long: number
  longChange: number | null
  short: number
  shortChange: number | null
}
interface InstrumentBlock {
  instrument: string
  netTotal: number
  netChange: number
  contracts: ContractLine[]
}

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
      longChange: null,
      short: 0,
      shortChange: null
    }
    const quantity = Number(row.quantity)
    const change = num(row.change)
    if (row.rank_type === 'long') {
      line.long += quantity
      line.longChange = (line.longChange ?? 0) + (change ?? 0)
    } else {
      line.short += quantity
      line.shortChange = (line.shortChange ?? 0) + (change ?? 0)
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
        netChange: lines.reduce(
          (sum, line) => sum + (line.longChange ?? 0) - (line.shortChange ?? 0),
          0
        ),
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

function signed(value: number | null) {
  if (value === null) return ''
  return value > 0 ? `+${value}` : String(value)
}
const fmt = (value: number) => value.toLocaleString('zh-CN')

// —— 建仓过程的三联图 ——
const dates = computed(() => days.value.map((day) => day.trade_date))
const netSeries = computed(() => days.value.map((day) => num(day.net_position)))
const costSeries = computed(() => days.value.map((day) => num(day.cost)))
const pnlSeries = computed(() => days.value.map((day) => num(day.daily_pnl)))
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
const gapDays = computed(() => days.value.filter((day) => day.cost === null).length)

const priceOption = computed<EChartsOption>(() => ({
  grid: { left: 60, right: 24, top: 24, bottom: 28 },
  tooltip: { trigger: 'axis' as const },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [
    {
      name: 'K线',
      type: 'candlestick' as const,
      data: candles.value as unknown as CandlestickSeriesOption['data']
    },
    {
      name: '净持仓成本（推算）',
      type: 'line' as const,
      data: costSeries.value,
      showSymbol: false,
      // 成本不可知的那几天必须断开，连线是画一条猜出来的线。
      connectNulls: false,
      lineStyle: { width: 2 }
    }
  ]
}))
const netOption = computed<EChartsOption>(() => ({
  grid: { left: 60, right: 24, top: 16, bottom: 28 },
  tooltip: { trigger: 'axis' as const },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [
    {
      name: '净持仓',
      type: 'line' as const,
      data: netSeries.value,
      showSymbol: false,
      connectNulls: false,
      areaStyle: {}
    }
  ]
}))
const pnlOption = computed<EChartsOption>(() => ({
  grid: { left: 60, right: 24, top: 16, bottom: 28 },
  tooltip: { trigger: 'axis' as const },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [{ name: '当日盈亏', type: 'bar' as const, data: pnlSeries.value }]
}))

const buildingContracts = computed(() => {
  const block = blocks.value.find((item) => item.instrument === buildingInstrument.value)
  return block ? block.contracts.map((line) => line.contract) : []
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
        <el-select
          v-model="tradeDate"
          style="width: 180px"
          filterable
          placeholder="交易日"
          :disabled="loadingPositions || !availableDates.length"
        >
          <el-option v-for="day in availableDates" :key="day" :label="day" :value="day" />
        </el-select>
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
          >
            <el-option
              v-for="code in instruments"
              :key="code"
              :label="varietyLabel(code)"
              :value="code"
            />
          </el-select>
          <el-select
            v-model="buildingContract"
            style="width: 190px"
            clearable
            placeholder="品种汇总"
            :disabled="loadingBuilding"
          >
            <el-option v-for="code in buildingContracts" :key="code" :label="code" :value="code" />
          </el-select>
          <span class="hint">
            {{ buildingContract ? '单合约' : '品种汇总' }}
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
      </el-card>

      <el-empty v-if="!loadingBuilding && !days.length" description="选一个品种，或该席位在此品种上没有持仓" />
      <template v-else>
        <el-card shadow="never">
          <template #header><h2>行情与成本线</h2></template>
          <SpreadChart v-if="hasCandles" :option="priceOption" :height="300" export-name="建仓过程-行情" />
          <el-alert
            v-else
            type="info"
            :closable="false"
            title="品种汇总没有单一合约的 K 线"
            description="把某个合约的 K 线安在品种汇总上会把两件事画成一件。选一个具体合约即可看到 K 线。"
          />
        </el-card>
        <el-card shadow="never">
          <template #header><h2>净持仓</h2></template>
          <SpreadChart :option="netOption" :height="200" export-name="建仓过程-净持仓" />
        </el-card>
        <el-card shadow="never">
          <template #header><h2>当日盈亏</h2></template>
          <SpreadChart :option="pnlOption" :height="200" export-name="建仓过程-当日盈亏" />
        </el-card>
      </template>
    </template>
  </section>
</template>

<style scoped>
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
