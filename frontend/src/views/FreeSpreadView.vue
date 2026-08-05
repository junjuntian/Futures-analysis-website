<script setup lang="ts">
import { Download, Plus, RefreshRight, Star } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import {
  ApiError,
  createSpreadFavorite,
  deleteSpreadFavorite,
  getSpreadFavorites,
  getSpreadMonths,
  getSpreadVarieties,
  queryFreeSpread,
  type FreeSpreadLeg,
  type FreeSpreadQueryResponse,
  type SpreadFavorite,
  type SpreadSourceMetadata,
  type SpreadVariety
} from '../api'
import SpreadChart from '../components/SpreadChart.vue'
import { continuousChartOption, formatNumber, seasonalChartOption } from '../spreadCharts'
import { useAuthStore } from '../stores/auth'

type LegKey = 'leg1' | 'leg2'
type ExportableChart = { download: (type: 'png' | 'svg') => void }

interface Preset {
  name: string
  leg1: { symbol: string; month: string }
  leg2: { symbol: string; month: string }
  disabledReason?: string
}

const presets: Preset[] = [
  { name: '卷螺差', leg1: { symbol: 'HC', month: '10' }, leg2: { symbol: 'RB', month: '10' } },
  { name: '豆棕差', leg1: { symbol: 'Y', month: '01' }, leg2: { symbol: 'P', month: '01' } },
  {
    name: '油粕比',
    leg1: { symbol: 'Y', month: '01' },
    leg2: { symbol: 'M', month: '01' },
    disabledReason: '上游口径待确认：三禾首期缺少两腿原价，暂不计算比值'
  },
  { name: '玻璃-纯碱', leg1: { symbol: 'FG', month: '01' }, leg2: { symbol: 'SA', month: '01' } },
  { name: '焦煤 9-1', leg1: { symbol: 'JM', month: '09' }, leg2: { symbol: 'JM', month: '01' } }
]

const auth = useAuthStore()
const varieties = ref<SpreadVariety[]>([])
const months = reactive<Record<LegKey, string[]>>({ leg1: [], leg2: [] })
const legs = reactive<Record<LegKey, FreeSpreadLeg>>({
  leg1: { variety: '', symbol: '', month: '' },
  leg2: { variety: '', symbol: '', month: '' }
})
const loadingVarieties = ref(false)
const loadingMonths = reactive<Record<LegKey, boolean>>({ leg1: false, leg2: false })
const querying = ref(false)
const result = ref<FreeSpreadQueryResponse>()
const source = ref<SpreadSourceMetadata>()
const favorites = ref<SpreadFavorite[]>([])
const errorMessage = ref('')
const favoriteDialog = ref(false)
const favoriteName = ref('')
const savingFavorite = ref(false)
const continuousChart = ref<ExportableChart>()
const seasonalChart = ref<ExportableChart>()

const markets = computed(() => {
  const grouped = new Map<string, SpreadVariety[]>()
  for (const item of varieties.value) {
    const items = grouped.get(item.market) ?? []
    items.push(item)
    grouped.set(item.market, items)
  }
  return Array.from(grouped, ([market, items]) => ({ market, items }))
})

const canQuery = computed(() => Object.values(legs).every((leg) => leg.variety && leg.symbol && leg.month))
const title = computed(() => result.value
  ? `价差走势 · ${result.value.query.leg1.symbol.toLowerCase()} ${result.value.query.leg1.month}−${result.value.query.leg2.symbol.toLowerCase()} ${result.value.query.leg2.month}`
  : '价差走势')
const subtitle = computed(() => result.value?.continuous_series.current_value == null
  ? '仅统计散户可交易窗口'
  : `仅统计散户可交易窗口 · 当前 ${formatNumber(result.value.continuous_series.current_value)}`)
const continuousOption = computed(() => result.value ? continuousChartOption(result.value) : {})
const seasonalOption = computed(() => result.value ? seasonalChartOption(result.value) : {})
const seasonalRange = computed(() => {
  const axis = result.value?.seasonal_series.axis ?? []
  if (!axis.length) return '—'
  return `${axis[0].replace('-', '/')}–${axis.at(-1)?.replace('-', '/')}`
})

function varietyByName(name: string) {
  return varieties.value.find((item) => item.name === name)
}

function varietyBySymbol(symbol: string) {
  return varieties.value.find((item) => item.symbol.toUpperCase() === symbol.toUpperCase())
}

async function ensureCsrf() {
  if (!auth.csrfToken) await auth.loadCsrf()
  if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
  return auth.csrfToken
}

async function changeVariety(key: LegKey, requestedMonth?: string) {
  const item = varietyByName(legs[key].variety)
  legs[key].symbol = item?.symbol.toUpperCase() ?? ''
  legs[key].month = ''
  months[key] = []
  if (!item) return
  loadingMonths[key] = true
  try {
    const envelope = await getSpreadMonths(item.name)
    months[key] = envelope.data.months
    source.value = envelope.data.source
    legs[key].month = requestedMonth && envelope.data.months.includes(requestedMonth)
      ? requestedMonth
      : (envelope.data.months[0] ?? '')
  } catch (error) {
    errorMessage.value = describeError(error)
  } finally {
    loadingMonths[key] = false
  }
}

async function applyPreset(preset: Preset) {
  if (preset.disabledReason) {
    ElMessage.info(preset.disabledReason)
    return
  }
  const first = varietyBySymbol(preset.leg1.symbol)
  const second = varietyBySymbol(preset.leg2.symbol)
  if (!first || !second) {
    ElMessage.warning('当前品种清单不包含此常用组合')
    return
  }
  legs.leg1.variety = first.name
  legs.leg2.variety = second.name
  await Promise.all([
    changeVariety('leg1', preset.leg1.month),
    changeVariety('leg2', preset.leg2.month)
  ])
}

async function applyFavorite(favorite: SpreadFavorite) {
  legs.leg1 = { ...favorite.leg1 }
  legs.leg2 = { ...favorite.leg2 }
  await Promise.all([
    changeVariety('leg1', favorite.leg1.month),
    changeVariety('leg2', favorite.leg2.month)
  ])
}

async function runQuery() {
  if (!canQuery.value) return
  querying.value = true
  errorMessage.value = ''
  try {
    const envelope = await queryFreeSpread({
      provider: 'sanhe',
      leg1: { ...legs.leg1 },
      leg2: { ...legs.leg2 }
    }, await ensureCsrf())
    result.value = envelope.data
    source.value = envelope.data.source
  } catch (error) {
    result.value = undefined
    errorMessage.value = describeError(error)
  } finally {
    querying.value = false
  }
}

async function loadFavorites() {
  try {
    favorites.value = (await getSpreadFavorites()).data
  } catch (error) {
    if (!(error instanceof ApiError && [401, 403].includes(error.status))) {
      ElMessage.warning(describeError(error))
    }
  }
}

function openFavoriteDialog() {
  if (!canQuery.value) {
    ElMessage.warning('请先选好两腿组合')
    return
  }
  favoriteName.value = `${legs.leg1.symbol}${legs.leg1.month}-${legs.leg2.symbol}${legs.leg2.month}`
  favoriteDialog.value = true
}

async function saveFavorite() {
  const name = favoriteName.value.trim()
  if (!name) return
  savingFavorite.value = true
  try {
    await createSpreadFavorite({
      name,
      provider: 'sanhe',
      leg1: { ...legs.leg1 },
      leg2: { ...legs.leg2 }
    }, await ensureCsrf())
    await loadFavorites()
    favoriteDialog.value = false
    ElMessage.success('已加入自定义收藏')
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    savingFavorite.value = false
  }
}

async function removeFavorite(favorite: SpreadFavorite) {
  try {
    await deleteSpreadFavorite(favorite.id, await ensureCsrf())
    favorites.value = favorites.value.filter((item) => item.id !== favorite.id)
  } catch (error) {
    ElMessage.error(describeError(error))
  }
}

function describeError(error: unknown) {
  if (error instanceof ApiError) {
    const known: Record<string, string> = {
      spread_provider_unavailable: '三禾数据暂时不可用，请稍后再试',
      spread_provider_rate_limited: '数据源正在限频，请稍后再试',
      spread_provider_forbidden: '三禾只读接口当前拒绝访问',
      spread_provider_contract_changed: '三禾接口格式发生变化，适配器已停止解析',
      favorite_exists: '该组合已在收藏中'
    }
    return known[error.code] ?? `请求失败（${error.code}）`
  }
  return error instanceof Error ? error.message : '请求失败'
}

function cellClass(delta?: number | null) {
  if (delta === null || delta === undefined) return 'matrix-empty'
  return delta > 0 ? 'matrix-up' : delta < 0 ? 'matrix-down' : 'matrix-flat'
}

function dateTime(value?: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

onMounted(async () => {
  loadingVarieties.value = true
  errorMessage.value = ''
  try {
    const [varietyEnvelope] = await Promise.all([getSpreadVarieties(), loadFavorites()])
    varieties.value = varietyEnvelope.data.items
    source.value = varietyEnvelope.data.source
    const defaultPreset = presets.find((preset) => preset.name === '焦煤 9-1')!
    if (varieties.value.length) {
      if (varietyBySymbol('JM')) await applyPreset(defaultPreset)
      else {
        legs.leg1.variety = varieties.value[0].name
        legs.leg2.variety = varieties.value[0].name
        await Promise.all([changeVariety('leg1'), changeVariety('leg2')])
      }
    }
  } catch (error) {
    errorMessage.value = describeError(error)
  } finally {
    loadingVarieties.value = false
  }
})
</script>

<template>
  <div class="free-spread-page">
    <div class="spread-breadcrumb">套利分析 <span>›</span> 自由价差</div>

    <section class="spread-card controls-card" v-loading="loadingVarieties">
      <div class="selector-row">
        <el-select v-model="legs.leg1.variety" filterable placeholder="第一腿品种" class="variety-select"
          @change="changeVariety('leg1')">
          <el-option-group v-for="group in markets" :key="group.market" :label="group.market">
            <el-option v-for="item in group.items" :key="item.name" :label="`${item.name} ${item.symbol}`" :value="item.name" />
          </el-option-group>
        </el-select>
        <el-select v-model="legs.leg1.month" placeholder="月份" class="month-select" :loading="loadingMonths.leg1">
          <el-option v-for="month in months.leg1" :key="month" :label="`${month} 合约`" :value="month" />
        </el-select>
        <span class="spread-minus">−</span>
        <el-select v-model="legs.leg2.variety" filterable placeholder="第二腿品种" class="variety-select"
          @change="changeVariety('leg2')">
          <el-option-group v-for="group in markets" :key="group.market" :label="group.market">
            <el-option v-for="item in group.items" :key="item.name" :label="`${item.name} ${item.symbol}`" :value="item.name" />
          </el-option-group>
        </el-select>
        <el-select v-model="legs.leg2.month" placeholder="月份" class="month-select" :loading="loadingMonths.leg2">
          <el-option v-for="month in months.leg2" :key="month" :label="`${month} 合约`" :value="month" />
        </el-select>
        <el-button type="primary" class="query-button" :loading="querying" :disabled="!canQuery" @click="runQuery">查看</el-button>
      </div>

      <div class="preset-row">
        <span class="preset-label">常用</span>
        <el-tooltip v-for="preset in presets" :key="preset.name" :disabled="!preset.disabledReason"
          :content="preset.disabledReason" placement="top">
          <el-button round :disabled="Boolean(preset.disabledReason)" @click="applyPreset(preset)">{{ preset.name }}</el-button>
        </el-tooltip>
        <el-tag v-for="favorite in favorites" :key="favorite.id" closable round class="favorite-pill"
          @click="applyFavorite(favorite)" @close.stop="removeFavorite(favorite)">
          {{ favorite.name }}
        </el-tag>
        <el-button text :icon="Plus" @click="openFavoriteDialog">自定义收藏</el-button>
      </div>
    </section>

    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" show-icon>
      <template #default>
        <el-button text :icon="RefreshRight" @click="runQuery">重试当前组合</el-button>
      </template>
    </el-alert>

    <template v-if="result">
      <el-alert v-if="result.quality.missing_contract_point_count" class="quality-alert" type="warning" :closable="false"
        title="部分点缺少本库合约或交易日历元数据，已按契约排除，未作猜测。" show-icon />

      <section class="spread-card chart-card">
        <div class="section-heading">
          <div>
            <h1>{{ title }}</h1>
            <p>按服务端散户可交易窗口截取后，以交易日拼接连续轴；虚线为换段边界。</p>
          </div>
          <div class="heading-meta">
            <strong>{{ subtitle }}</strong>
            <span>数据来源：{{ result.source.source_display_name }}</span>
            <span>取数时间：{{ dateTime(result.source.fetched_at) }}</span>
          </div>
        </div>
        <div v-if="result.continuous_series.points.length" class="chart-wrap">
          <SpreadChart ref="continuousChart" :option="continuousOption" :height="390" export-name="自由价差-连续走势" />
          <div class="export-actions">
            <el-button size="small" :icon="Download" @click="continuousChart?.download('png')">PNG</el-button>
            <el-button size="small" :icon="Download" @click="continuousChart?.download('svg')">SVG</el-button>
          </div>
        </div>
        <el-empty v-else description="该组合在散户可交易窗口内暂无数据" />
        <div class="chart-footnote">
          保留 {{ result.quality.retained_point_count }} 点，剔除 {{ result.quality.excluded_point_count }} 点 ·
          窗口算法 {{ result.algorithm_versions.window }} · 规则 {{ result.algorithm_versions.rule }}
        </div>
      </section>

      <section class="spread-card analytics-card">
        <div class="section-heading compact">
          <div>
            <h2>季节叠年图</h2>
            <p>图例可点选年份；当年曲线加粗。</p>
          </div>
          <div class="heading-meta"><strong>日历轴 {{ seasonalRange }}</strong></div>
        </div>
        <div v-if="result.seasonal_series.years.length" class="chart-wrap">
          <SpreadChart ref="seasonalChart" :option="seasonalOption" :height="340" export-name="自由价差-季节叠年" />
          <div class="export-actions">
            <el-button size="small" :icon="Download" @click="seasonalChart?.download('png')">PNG</el-button>
            <el-button size="small" :icon="Download" @click="seasonalChart?.download('svg')">SVG</el-button>
          </div>
        </div>
        <el-empty v-else description="没有足够的可交易窗口数据生成季节图" />

        <el-divider />

        <div class="section-heading compact matrix-heading">
          <div>
            <h2>月度涨跌矩阵</h2>
            <p>月度变化为窗口内当月末值减月初值；“—”表示不可交易或样本不足。</p>
          </div>
          <div class="matrix-legend"><span class="up-dot" />红涨 <span class="down-dot" />绿跌</div>
        </div>
        <div class="matrix-scroll">
          <table class="monthly-matrix">
            <thead>
              <tr><th>年份</th><th v-for="month in 12" :key="month">{{ month }}月</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in result.monthly_matrix.years" :key="row.year">
                <th>{{ row.year }}</th>
                <td v-for="cell in row.months" :key="cell.month" :class="cellClass(cell.delta)">
                  <template v-if="cell.delta !== null && cell.delta !== undefined">
                    {{ cell.delta > 0 ? '+' : '' }}{{ formatNumber(cell.delta) }}<sup v-if="cell.is_partial">*</sup>
                  </template>
                  <template v-else>—</template>
                </td>
              </tr>
              <tr class="ratio-row">
                <th>上涨占比</th>
                <td v-for="ratio in result.monthly_matrix.up_ratios" :key="ratio.month">
                  {{ ratio.ratio === null || ratio.ratio === undefined ? '—' : `${Math.round(ratio.ratio * 100)}%` }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="chart-footnote">统计算法 {{ result.algorithm_versions.statistics }} · * 为数据截止月</div>
      </section>
    </template>

    <section v-else-if="!querying" class="spread-card initial-empty">
      <el-empty description="选择两腿品种与月份后查看自由价差">
        <el-button type="primary" :disabled="!canQuery" @click="runQuery">查看当前组合</el-button>
      </el-empty>
    </section>

    <footer class="source-footer">
      <span><el-icon><Star /></el-icon> 数据来源：{{ source?.source_display_name ?? '三禾数据' }}</span>
      <span>服务端代理 · 同参数当日缓存 · 原始统计未直接采用</span>
    </footer>

    <el-dialog v-model="favoriteDialog" title="保存自定义组合" width="420px">
      <el-input v-model="favoriteName" maxlength="80" show-word-limit placeholder="收藏名称" @keyup.enter="saveFavorite" />
      <template #footer>
        <el-button @click="favoriteDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingFavorite" @click="saveFavorite">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.free-spread-page { max-width: 1480px; margin: 0 auto; color: #191918; }
.spread-breadcrumb { margin: 2px 0 18px; color: #8d8c87; font-size: 15px; }
.spread-breadcrumb span { margin: 0 8px; color: #b6b5b1; }
.spread-card { background: #fff; border: 1px solid #e4e3df; border-radius: 20px; box-shadow: 0 7px 24px rgba(34, 31, 26, .035); }
.controls-card { padding: 28px 30px 23px; margin-bottom: 18px; }
.selector-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.variety-select { width: 230px; }
.month-select { width: 150px; }
.spread-minus { color: #8d8c87; font-size: 26px; }
.query-button { min-width: 100px; height: 42px; }
.preset-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.preset-label { color: #8d8c87; margin-right: 2px; }
.favorite-pill { cursor: pointer; height: 32px; padding: 0 12px; }
.quality-alert { margin: 0 0 18px; }
.chart-card, .analytics-card { padding: 28px 30px 22px; margin-top: 18px; }
.section-heading { display: flex; justify-content: space-between; gap: 28px; align-items: flex-start; }
.section-heading h1, .section-heading h2 { margin: 0; color: #141413; }
.section-heading h1 { font-size: 25px; }
.section-heading h2 { font-size: 23px; }
.section-heading p { margin: 8px 0 0; color: #8d8c87; font-size: 14px; }
.section-heading.compact { margin-bottom: 2px; }
.heading-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; color: #8d8c87; font-size: 13px; white-space: nowrap; }
.heading-meta strong { color: #8d8c87; font-size: 16px; font-weight: 500; }
.chart-wrap { position: relative; margin-top: 10px; }
.export-actions { position: absolute; right: 2px; bottom: 3px; display: flex; gap: 4px; }
.chart-footnote { margin-top: 12px; color: #8d8c87; font-size: 13px; }
.matrix-heading { align-items: center; }
.matrix-legend { color: #8d8c87; white-space: nowrap; }
.up-dot, .down-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 5px 0 12px; }
.up-dot { background: #ed8b8b; }
.down-dot { background: #86bd72; }
.matrix-scroll { overflow-x: auto; margin-top: 18px; }
.monthly-matrix { width: 100%; min-width: 1080px; border-collapse: separate; border-spacing: 0; text-align: center; font-variant-numeric: tabular-nums; }
.monthly-matrix th, .monthly-matrix td { padding: 13px 9px; border-bottom: 1px solid #f0efec; }
.monthly-matrix thead th { color: #8d8c87; font-weight: 500; }
.monthly-matrix tbody th { text-align: left; color: #55534f; font-weight: 600; white-space: nowrap; }
.monthly-matrix td { color: #595752; }
.monthly-matrix .matrix-up { background: #f8d1d1; color: #9d3030; }
.monthly-matrix .matrix-down { background: #cce8c9; color: #376f1c; }
.monthly-matrix .matrix-flat { background: #f0efec; }
.monthly-matrix .matrix-empty { color: #aaa9a5; background: #fff; }
.monthly-matrix sup { margin-left: 2px; color: #d97706; }
.ratio-row td, .ratio-row th { border-bottom: 0; font-weight: 600; background: #fff; }
.initial-empty { margin-top: 18px; min-height: 310px; display: grid; place-items: center; }
.source-footer { display: flex; justify-content: space-between; gap: 18px; padding: 18px 6px 4px; color: #8d8c87; font-size: 13px; }
.source-footer span { display: flex; align-items: center; gap: 5px; }
@media (max-width: 980px) {
  .section-heading { flex-direction: column; }
  .heading-meta { align-items: flex-start; white-space: normal; }
  .source-footer { flex-direction: column; }
}
</style>
