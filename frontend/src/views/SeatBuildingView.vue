<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getSeatBuilding, getSeatPositions, type BuildingDay } from '../api'
import type { CandlestickSeriesOption, EChartsOption } from 'echarts'
import SpreadChart from '../components/SpreadChart.vue'

const INSTRUMENTS = [
  { code: 'JM', name: '焦煤' },
  { code: 'JD', name: '鸡蛋' },
  { code: 'LH', name: '生猪' },
  { code: 'AP', name: '苹果' },
  { code: 'FG', name: '玻璃' },
  { code: 'SA', name: '纯碱' },
  { code: 'AU', name: '黄金' },
  { code: 'AG', name: '白银' }
]

const instrument = ref('JM')
const member = ref('')
const contract = ref('')
const members = ref<string[]>([])
const contracts = ref<string[]>([])
const days = ref<BuildingDay[]>([])
const multiplier = ref<string | null>(null)
const loading = ref(false)

async function loadContracts() {
  try {
    const { data } = await getSeatPositions(instrument.value)
    contracts.value = [
      ...new Set(data.rows.map((row) => row.contract).filter((code): code is string => !!code))
    ].sort()
  } catch {
    contracts.value = []
  }
}

async function load() {
  loading.value = true
  try {
    const { data } = await getSeatBuilding(instrument.value, member.value, contract.value || undefined)
    members.value = data.members
    multiplier.value = data.price_multiplier
    days.value = data.days
    if (!member.value && data.members.length) {
      member.value = data.members[0]
      await load()
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '建仓过程读取失败')
    days.value = []
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadContracts()
  await load()
})
watch(instrument, async () => {
  member.value = ''
  contract.value = ''
  await loadContracts()
  await load()
})
watch([member, contract], () => {
  if (member.value) load()
})

const dates = computed(() => days.value.map((day) => day.trade_date))
const num = (value: string | null) => (value === null || value === '' ? null : Number(value))

/** 单合约会因掉出前 20 而中断。断开显示，不连线——中间那几天数据本来就没有，
 *  连线是画一条猜出来的线。运营者定的。 */
const netSeries = computed(() => days.value.map((day) => num(day.net_position)))
const costSeries = computed(() => days.value.map((day) => num(day.cost)))
const pnlSeries = computed(() => days.value.map((day) => num(day.daily_pnl)))
const candles = computed(() =>
  days.value.map((day) => {
    const open = num(day.open_price)
    const close = num(day.close_price)
    const low = num(day.low_price)
    const high = num(day.high_price)
    // 缺一项就整根不画。掉出前 20 那几天本来就没有行情，
    // 补一根形状可疑的蜡烛比空着更糟。'-' 是 ECharts 自己的空值写法。
    return open === null || close === null || low === null || high === null
      ? '-'
      : [open, close, low, high]
  })
)

const hasCandles = computed(() => candles.value.some((item) => item !== '-'))
const gapDays = computed(() => days.value.filter((day) => day.cost === null).length)

const priceOption = computed<EChartsOption>(() => ({
  grid: { left: 56, right: 24, top: 24, bottom: 28 },
  tooltip: { trigger: 'axis' as const },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [
    {
      name: 'K线',
      type: 'candlestick' as const,
      // ECharts 运行时把 '-' 当空值，但它的类型定义里没有这一支，
      // 所以在这里收一次口，而不是为了迁就类型去画一根假蜡烛。
      data: candles.value as unknown as CandlestickSeriesOption['data']
    },
    {
      name: '净持仓成本（推算）',
      type: 'line' as const,
      data: costSeries.value,
      showSymbol: false,
      // connectNulls 保持 false：成本不可知的那几天必须断开。
      connectNulls: false,
      lineStyle: { width: 2 }
    }
  ]
}))

const netOption = computed<EChartsOption>(() => ({
  grid: { left: 56, right: 24, top: 16, bottom: 28 },
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
  grid: { left: 56, right: 24, top: 16, bottom: 28 },
  tooltip: { trigger: 'axis' as const },
  xAxis: { type: 'category' as const, data: dates.value, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value' as const, scale: true },
  series: [{ name: '当日盈亏', type: 'bar' as const, data: pnlSeries.value }]
}))
</script>

<template>
  <section class="seat-building">
    <header class="page-head">
      <div>
        <h1>建仓过程</h1>
        <p>某会员在某合约（或整个品种）上的逐日持仓、净持仓成本与盈亏。</p>
      </div>
    </header>

    <el-card shadow="never">
      <div class="control-row">
        <el-select v-model="instrument" style="width: 150px" :disabled="loading">
          <el-option
            v-for="item in INSTRUMENTS"
            :key="item.code"
            :label="`${item.name} ${item.code}`"
            :value="item.code"
          />
        </el-select>
        <el-select
          v-model="member"
          style="width: 200px"
          filterable
          placeholder="选择会员"
          :disabled="loading"
        >
          <el-option v-for="name in members" :key="name" :label="name" :value="name" />
        </el-select>
        <el-select
          v-model="contract"
          style="width: 190px"
          clearable
          placeholder="品种汇总"
          :disabled="loading"
        >
          <el-option v-for="code in contracts" :key="code" :label="code" :value="code" />
        </el-select>
        <span class="hint">
          {{ contract ? '单合约' : '品种汇总' }}
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

    <el-empty v-if="!loading && !days.length" description="这个会员在该品种上没有持仓记录" />

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
  </section>
</template>

<style scoped>
.seat-building {
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
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.seat-building h2 {
  margin: 0;
  font-size: 16px;
}
</style>
