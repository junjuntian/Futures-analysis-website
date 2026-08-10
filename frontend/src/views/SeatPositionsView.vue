<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getSeatPositions, type SeatPositionRow } from '../api'

// 运营者要的八个品种。名字写在这里而不是从接口取：这一页是为这八个做的，
// 品种列表变动是一件需要有人过目的事，不该悄悄跟着上游变。
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
const RANK_LABEL: Record<string, string> = { volume: '成交量', long: '持买', short: '持卖' }

const instrument = ref('JM')
const tradeDate = ref<string>('')
const availableDates = ref<string[]>([])
const coverageStart = ref<string | null>(null)
const rows = ref<SeatPositionRow[]>([])
const loading = ref(false)

async function load(date?: string) {
  loading.value = true
  try {
    const { data } = await getSeatPositions(instrument.value, date)
    rows.value = data.rows
    availableDates.value = data.available_dates
    coverageStart.value = data.coverage_start
    tradeDate.value = data.trade_date ?? ''
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '席位数据读取失败')
    rows.value = []
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
watch(instrument, () => load())

/** 品种汇总与逐合约分开：一个是该品种整体，一个是具体合约，混在一张表里没法看。 */
const varietyTotals = computed(() => rows.value.filter((row) => row.is_variety_total))
const byContract = computed(() => {
  const groups = new Map<string, SeatPositionRow[]>()
  for (const row of rows.value) {
    if (row.is_variety_total || !row.contract) continue
    const list = groups.get(row.contract) ?? []
    list.push(row)
    groups.set(row.contract, list)
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
})

/** 一个榜（成交量／持买／持卖）按名次排好。 */
function board(list: SeatPositionRow[], kind: string) {
  return list
    .filter((row) => row.rank_type === kind)
    .sort((a, b) => (a.rank ?? 9999) - (b.rank ?? 9999))
}

function signed(value: string | null) {
  if (value === null || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return value
  return number > 0 ? `+${number}` : String(number)
}

function amount(value: string) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toLocaleString('zh-CN') : value
}

/** 有的来源不给名次（三禾只给持买持卖两个数），如实显示为空而不是编一个。 */
function rankText(row: SeatPositionRow) {
  return row.rank === null ? '—' : String(row.rank)
}

const sources = computed(() => [...new Set(rows.value.map((row) => row.source))].join('、'))
</script>

<template>
  <section class="seat-positions">
    <header class="page-head">
      <div>
        <h1>席位每日持仓</h1>
        <p>选品种、选日期，看当天各会员的成交量与多空持仓。</p>
      </div>
    </header>

    <el-card shadow="never" class="controls">
      <div class="control-row">
        <el-select v-model="instrument" style="width: 160px" :disabled="loading">
          <el-option
            v-for="item in INSTRUMENTS"
            :key="item.code"
            :label="`${item.name} ${item.code}`"
            :value="item.code"
          />
        </el-select>
        <el-select
          v-model="tradeDate"
          style="width: 180px"
          :disabled="loading || !availableDates.length"
          placeholder="交易日"
          filterable
          @change="load(tradeDate)"
        >
          <el-option v-for="day in availableDates" :key="day" :label="day" :value="day" />
        </el-select>
        <span v-if="coverageStart" class="coverage">
          该品种席位数据自 <strong>{{ coverageStart }}</strong> 起
        </span>
        <span v-if="sources" class="coverage">来源：{{ sources }}</span>
      </div>
    </el-card>

    <el-empty v-if="!loading && !rows.length" description="这一天没有席位数据" />

    <template v-else>
      <el-card v-if="varietyTotals.length" shadow="never" class="panel">
        <template #header>
          <div class="panel-head">
            <h2>品种汇总</h2>
            <span v-if="varietyTotals[0]?.variety_total_is_computed" class="computed">
              由各合约加总得出，非交易所公布
            </span>
            <span v-else class="official">交易所公布</span>
          </div>
        </template>
        <div class="boards">
          <div v-for="kind in ['volume', 'long', 'short']" :key="kind" class="board">
            <h3>{{ RANK_LABEL[kind] }}</h3>
            <el-table :data="board(varietyTotals, kind)" size="small" :max-height="360">
              <el-table-column prop="rank" label="名次" width="60">
                <template #default="{ row }">{{ rankText(row) }}</template>
              </el-table-column>
              <el-table-column prop="member" label="会员" min-width="120" />
              <el-table-column label="手数" width="100" align="right">
                <template #default="{ row }">{{ amount(row.quantity) }}</template>
              </el-table-column>
              <el-table-column label="增减" width="90" align="right">
                <template #default="{ row }">{{ signed(row.change) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-card>

      <el-card v-for="[contract, list] in byContract" :key="contract" shadow="never" class="panel">
        <template #header>
          <h2>{{ contract }}</h2>
        </template>
        <div class="boards">
          <div v-for="kind in ['volume', 'long', 'short']" :key="kind" class="board">
            <h3>{{ RANK_LABEL[kind] }}</h3>
            <el-table :data="board(list, kind)" size="small" :max-height="360">
              <el-table-column prop="rank" label="名次" width="60">
                <template #default="{ row }">{{ rankText(row) }}</template>
              </el-table-column>
              <el-table-column prop="member" label="会员" min-width="120" />
              <el-table-column label="手数" width="100" align="right">
                <template #default="{ row }">{{ amount(row.quantity) }}</template>
              </el-table-column>
              <el-table-column label="增减" width="90" align="right">
                <template #default="{ row }">{{ signed(row.change) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-card>
    </template>
  </section>
</template>

<style scoped>
.seat-positions {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.page-head p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.control-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.coverage {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.panel-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.panel h2 {
  margin: 0;
  font-size: 16px;
}
.computed {
  font-size: 12px;
  color: var(--el-color-warning);
}
.official {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.boards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
}
.board h3 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}
</style>
