<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  getSpreadMonitor,
  getSpreadVarieties,
  type SpreadMonitorItem,
  type SpreadMonitorTrack
} from '../api'

// 阈值：落在区间两端多少算触发。括号里是 2026-08-11 生产快照上的真实触发数（共 91 组），
// 后端 MONITOR_THRESHOLD_DEFAULT 有完整的量测表与选 5% 的理由。
const THRESHOLDS = [
  { value: 0.03, label: '3% · 最严' },
  { value: 0.05, label: '5% · 默认（约 25 组）' },
  { value: 0.1, label: '10% · 报得多（约 47 组）' },
  { value: 0.2, label: '20% · 几乎全报（约 61 组）' }
]

const threshold = ref(0.05)
const tradeDate = ref('')
const direction = ref<'all' | 'high' | 'low'>('all')
const varietyFilter = ref<string[]>([])
const showQuiet = ref(false)

const items = ref<SpreadMonitorItem[]>([])
const asOf = ref<string | null>(null)
const availableDates = ref<string[]>([])
const loading = ref(false)

// 品种中文名取自 product_instrument_scope，与套利页、席位页同一个来源——
// 三个页面显示同一个名字，不各写一份。
const varietyNames = ref<Record<string, string>>({})
const router = useRouter()

async function loadVarietyNames() {
  try {
    const { data } = await getSpreadVarieties('self')
    varietyNames.value = Object.fromEntries(data.items.map((item) => [item.symbol, item.name]))
  } catch {
    // 取不到就退回代码。少个中文名，好过因为一次取名失败让整张表打不开。
  }
}

async function load() {
  loading.value = true
  try {
    const { data } = await getSpreadMonitor(threshold.value, tradeDate.value || undefined)
    items.value = data.items
    asOf.value = data.as_of
    availableDates.value = data.available_dates
    if (!tradeDate.value && data.as_of) tradeDate.value = data.as_of
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '套利监控读取失败')
    items.value = []
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadVarietyNames()
  void load()
})
watch([threshold, tradeDate], () => void load())

const availableDateSet = computed(() => new Set(availableDates.value))
function isNotTradingDay(day: Date) {
  const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
  return !availableDateSet.value.has(key)
}

function label(instrument: string) {
  return varietyNames.value[instrument] ?? instrument
}
function comboName(item: SpreadMonitorItem) {
  return item.is_cross_variety
    ? `${label(item.instrument_1)} ${item.contract_1} − ${label(item.instrument_2)} ${item.contract_2}`
    : `${item.contract_1} − ${item.contract_2}`
}

const varieties = computed(() => {
  const seen = new Set<string>()
  for (const item of items.value) {
    seen.add(item.instrument_1)
    seen.add(item.instrument_2)
  }
  return [...seen].sort()
})

/** 离中线多远。越大越极端；排序靠它，越极端的排越前。 */
function extremity(item: SpreadMonitorItem) {
  const values = [item.pair, item.years]
    .filter((track): track is SpreadMonitorTrack => Boolean(track))
    .map((track) => (track.position === null ? null : Math.abs(Number(track.position) - 0.5)))
    .filter((value): value is number => value !== null && Number.isFinite(value))
  return values.length ? Math.max(...values) : -1
}

const filtered = computed(() => {
  const wanted = new Set(varietyFilter.value)
  return items.value
    .filter((item) => {
      if (wanted.size && !wanted.has(item.instrument_1) && !wanted.has(item.instrument_2)) {
        return false
      }
      if (direction.value !== 'all' && item.alert !== direction.value) return false
      return true
    })
    .sort((a, b) => extremity(b) - extremity(a))
})

const fired = computed(() => filtered.value.filter((item) => item.alert))
const quiet = computed(() => filtered.value.filter((item) => !item.alert))
const highCount = computed(() => fired.value.filter((item) => item.alert === 'high').length)
const lowCount = computed(() => fired.value.filter((item) => item.alert === 'low').length)

/** 圆点在轨道上的位置。位置可以落在 0~1 之外（历年轨用的是百分位区间），画到边上为止。 */
function markLeft(track: SpreadMonitorTrack) {
  if (track.position === null) return null
  const value = Number(track.position)
  if (!Number.isFinite(value)) return null
  return `${Math.min(100, Math.max(0, value * 100))}%`
}
function pct(track: SpreadMonitorTrack) {
  if (track.position === null) return '—'
  const value = Number(track.position)
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'
}
function num(text: string) {
  const value = Number(text)
  return Number.isFinite(value) ? value.toLocaleString('zh-CN') : text
}
function signedSpread(text: string) {
  const value = Number(text)
  if (!Number.isFinite(value)) return text
  return value > 0 ? `+${value.toLocaleString('zh-CN')}` : value.toLocaleString('zh-CN')
}

function openDetail(item: SpreadMonitorItem) {
  void router.push({
    path: '/spread-analytics/free-spread',
    query: {
      provider: 'self',
      variety1: item.instrument_1,
      month1: item.contract_1.slice(-2),
      variety2: item.instrument_2,
      month2: item.contract_2.slice(-2)
    }
  })
}
</script>

<template>
  <section class="page monitor">
    <header class="page-heading">
      <h1>套利监控</h1>
      <p>
        盯住每一组合约的价差，看它是不是快到历史极值了。触发与否是按下面的阈值现算的，
        换个阈值同一天的结论就跟着变。
      </p>
    </header>

    <el-card shadow="never" class="controls">
      <div class="control-row">
        <el-select
          v-model="varietyFilter"
          multiple
          collapse-tags
          collapse-tags-tooltip
          clearable
          placeholder="全部品种"
          style="width: 220px"
        >
          <el-option v-for="code in varieties" :key="code" :label="label(code)" :value="code" />
        </el-select>

        <el-select v-model="threshold" style="width: 190px">
          <el-option v-for="opt in THRESHOLDS" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>

        <el-date-picker
          v-model="tradeDate"
          type="date"
          style="width: 170px"
          placeholder="交易日"
          value-format="YYYY-MM-DD"
          :clearable="false"
          :disabled-date="isNotTradingDay"
          :disabled="!availableDates.length"
        />

        <el-radio-group v-model="direction">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="high">高位</el-radio-button>
          <el-radio-button value="low">低位</el-radio-button>
        </el-radio-group>
      </div>

      <div class="tally">
        <div class="cell"><span class="k">监控组合</span><span class="v">{{ items.length }}</span></div>
        <div class="cell"><span class="k">触发</span><span class="v">{{ fired.length }}</span></div>
        <div class="cell"><span class="k">高位</span><span class="v high">{{ highCount }}</span></div>
        <div class="cell"><span class="k">低位</span><span class="v low">{{ lowCount }}</span></div>
        <div class="cell" v-if="asOf"><span class="k">数据日</span><span class="v date">{{ asOf }}</span></div>
      </div>
    </el-card>

    <el-empty v-if="!loading && !items.length" description="这一天还没有监控快照" />

    <template v-else>
      <h2 class="group-label">触发中</h2>
      <el-empty v-if="!fired.length" description="按当前阈值没有组合触发" :image-size="70" />
      <div class="rows" v-loading="loading">
        <article
          v-for="item in fired"
          :key="item.contract_1 + item.contract_2"
          class="row"
          :class="item.alert === 'high' ? 'fired-high' : 'fired-low'"
        >
          <div class="ident">
            <div class="pair">{{ comboName(item) }}</div>
            <div class="meta">
              <el-tag size="small" :type="item.is_cross_variety ? 'warning' : 'info'" effect="plain">
                {{ item.is_cross_variety ? '跨品种' : label(item.instrument_1) }}
              </el-tag>
              <span class="asof" v-if="item.trade_date !== asOf">{{ item.trade_date }}</span>
            </div>
            <div class="now">{{ signedSpread(item.spread) }}</div>
          </div>

          <div class="tracks">
            <div class="track" v-for="t in [{ name: '当年', d: item.pair }, { name: '历年', d: item.years }]" :key="t.name">
              <span class="name">{{ t.name }}</span>
              <div v-if="t.d">
                <div class="rail">
                  <span class="zone lo" :style="{ width: `${threshold * 100}%` }"></span>
                  <span class="zone hi" :style="{ width: `${threshold * 100}%` }"></span>
                  <span
                    v-if="markLeft(t.d)"
                    class="mark"
                    :class="t.d.alert ?? ''"
                    :style="{ left: markLeft(t.d)! }"
                  ></span>
                </div>
                <div class="ends"><span>{{ num(t.d.low) }}</span><span>{{ num(t.d.high) }}</span></div>
              </div>
              <div v-else class="absent">历史不足</div>
              <span class="pct" :class="t.d?.alert ?? ''">{{ t.d ? pct(t.d) : '—' }}</span>
            </div>
          </div>

          <div class="tail">
            <el-tag :type="item.alert === 'high' ? 'danger' : 'success'" effect="light" size="small">
              {{ item.alert === 'high' ? '高位' : '低位' }}
            </el-tag>
            <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
          </div>
        </article>
      </div>

      <h2 class="group-label" v-if="quiet.length">
        未触发
        <el-button link type="primary" size="small" @click="showQuiet = !showQuiet">
          {{ showQuiet ? '收起' : `展开 ${quiet.length} 组` }}
        </el-button>
      </h2>
      <div class="rows" v-if="showQuiet">
        <article v-for="item in quiet" :key="item.contract_1 + item.contract_2" class="row quiet">
          <div class="ident">
            <div class="pair">{{ comboName(item) }}</div>
            <div class="meta">
              <el-tag size="small" :type="item.is_cross_variety ? 'warning' : 'info'" effect="plain">
                {{ item.is_cross_variety ? '跨品种' : label(item.instrument_1) }}
              </el-tag>
            </div>
            <div class="now">{{ signedSpread(item.spread) }}</div>
          </div>
          <div class="tracks">
            <div class="track" v-for="t in [{ name: '当年', d: item.pair }, { name: '历年', d: item.years }]" :key="t.name">
              <span class="name">{{ t.name }}</span>
              <div v-if="t.d">
                <div class="rail">
                  <span class="zone lo" :style="{ width: `${threshold * 100}%` }"></span>
                  <span class="zone hi" :style="{ width: `${threshold * 100}%` }"></span>
                  <span v-if="markLeft(t.d)" class="mark" :style="{ left: markLeft(t.d)! }"></span>
                </div>
                <div class="ends"><span>{{ num(t.d.low) }}</span><span>{{ num(t.d.high) }}</span></div>
              </div>
              <div v-else class="absent">历史不足</div>
              <span class="pct">{{ t.d ? pct(t.d) : '—' }}</span>
            </div>
          </div>
          <div class="tail">
            <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.controls {
  margin-bottom: 20px;
}
.control-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.tally {
  display: flex;
  flex-wrap: wrap;
  gap: 28px;
  margin-top: 16px;
}
.tally .cell {
  display: flex;
  flex-direction: column;
}
.tally .k {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: #8d99a8;
  font-weight: 650;
}
.tally .v {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.tally .v.high {
  color: #c0392b;
}
.tally .v.low {
  color: #1e7a43;
}
.tally .v.date {
  font-size: 16px;
  font-weight: 600;
  padding-top: 5px;
}

.group-label {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.06em;
  color: #8d99a8;
  margin: 22px 0 10px;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.row {
  display: grid;
  grid-template-columns: minmax(190px, 1fr) minmax(300px, 2.1fr) auto;
  gap: 22px;
  align-items: center;
  padding: 15px 17px;
  background: #fff;
  border: 1px solid #e5eaf3;
  border-radius: 8px;
}
.row.fired-high {
  border-left: 3px solid #c0392b;
}
.row.fired-low {
  border-left: 3px solid #1e7a43;
}
.row.quiet {
  background: #fafbfd;
}

.ident {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.ident .pair {
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.ident .meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ident .asof {
  font-size: 11px;
  color: #8d99a8;
}
.ident .now {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

.tracks {
  display: flex;
  flex-direction: column;
  gap: 11px;
  min-width: 0;
}
.track {
  display: grid;
  grid-template-columns: 40px 1fr 58px;
  gap: 11px;
  align-items: center;
}
.track .name {
  font-size: 11px;
  color: #8d99a8;
  font-weight: 650;
}
.rail {
  position: relative;
  height: 22px;
  border-radius: 4px;
  background: #fafbfd;
  border: 1px solid #e5eaf3;
}
.zone {
  position: absolute;
  top: 0;
  bottom: 0;
}
.zone.hi {
  right: 0;
  background: #fbeceb;
  border-radius: 0 3px 3px 0;
}
.zone.lo {
  left: 0;
  background: #e9f6ee;
  border-radius: 3px 0 0 3px;
}
.mark {
  position: absolute;
  top: 50%;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
  border: 2px solid #fff;
  background: #8d99a8;
  box-shadow: 0 0 0 1px #d3dced;
}
.mark.high {
  background: #c0392b;
  box-shadow: 0 0 0 1px #c0392b;
}
.mark.low {
  background: #1e7a43;
  box-shadow: 0 0 0 1px #1e7a43;
}
.ends {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: #8d99a8;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.absent {
  font-size: 11px;
  color: #b4bdc8;
}
.pct {
  text-align: right;
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #5b6876;
}
.pct.high {
  color: #c0392b;
}
.pct.low {
  color: #1e7a43;
}

.tail {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

@media (max-width: 880px) {
  .row {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .tail {
    flex-direction: row;
    align-items: flex-start;
  }
}
</style>
