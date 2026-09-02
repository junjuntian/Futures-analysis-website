<script setup lang="ts">
import { Download, Plus, RefreshRight, Star } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
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
import { failureHint, isNetworkFailure } from '../fetch-hint'
import { continuousChartOption, formatNumber, seasonalChartOption } from '../spreadCharts'
import { useAuthStore } from '../stores/auth'

type LegKey = 'leg1' | 'leg2'
type ExportableChart = { download: (type: 'png' | 'svg') => void }

const auth = useAuthStore()
// useRoute 必须在 setup 同步阶段调用,放进 onMounted 的 await 之后就拿不到实例了。
const route = useRoute()
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
/** 两腿品种不同 = 跨品种(DEC-161 起放行),提示报价单位需可比。 */
const crossVariety = computed(
  () => !!legs.leg1.symbol && !!legs.leg2.symbol && legs.leg1.symbol !== legs.leg2.symbol
)
const title = computed(() => result.value
  ? `价差走势 · ${result.value.query.leg1.symbol.toLowerCase()} ${result.value.query.leg1.month}−${result.value.query.leg2.symbol.toLowerCase()} ${result.value.query.leg2.month}`
  : '价差走势')
const subtitle = computed(() => result.value?.continuous_series.current_value == null
  ? '仅统计散户可交易窗口'
  : `仅统计散户可交易窗口 · 当前 ${formatNumber(result.value.continuous_series.current_value)}`)
const continuousOption = computed(() => result.value ? continuousChartOption(result.value) : {})
const seasonalOption = computed(() => result.value ? seasonalChartOption(result.value) : {})
// 取数时间 is when we called the upstream, which says nothing about how fresh
// the upstream itself is, nor about where the retail window cuts the series
// off. Both were being read as "the data is stale", so surface them.
const lastPoint = computed(() => result.value?.continuous_series.points.at(-1))
const activeWindowEnd = computed(() => result.value?.segments
  .find((segment) => segment.segment_no === lastPoint.value?.segment_no)?.window_end ?? null)
const seasonalRange = computed(() => {
  const axis = result.value?.seasonal_series.axis ?? []
  if (!axis.length) return '—'
  return `${axis[0].replace('-', '/')}–${axis.at(-1)?.replace('-', '/')}`
})

// e.g. 焦煤jm09 — the variety name alone is ambiguous once two legs share it,
// and the bare code alone (jm09) does not say which variety it is.
function legLabel(key: LegKey) {
  return `${legs[key].variety}${legs[key].symbol.toLowerCase()}${legs[key].month}`
}

function varietyByName(name: string) {
  return varieties.value.find((item) => item.name === name)
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

async function applyFavorite(favorite: SpreadFavorite) {
  legs.leg1 = { ...favorite.leg1 }
  legs.leg2 = { ...favorite.leg2 }
  await Promise.all([
    changeVariety('leg1', favorite.leg1.month),
    changeVariety('leg2', favorite.leg2.month)
  ])
}

async function queryOnce() {
  return queryFreeSpread({
    provider: 'self',
    leg1: { ...legs.leg1 },
    leg2: { ...legs.leg2 }
  }, await ensureCsrf())
}

/**
 * 查到就渲染;**网络层失败自动重发一次**。
 *
 * 2026-09-02:页面报 `Failed to fetch`,而 qh 上两级 nginx(宿主 443 + 容器)
 * 的 access log 里连一行都没有、error log 全空,同一组合手工重放 396ms 就回
 * 200 —— 请求根本没送到服务器。站点走 HTTP/2,nginx 没配 keepalive_timeout
 * 走默认 65 秒;看着图琢磨几分钟再点「查看」,浏览器正好复用那条已被服务器
 * 关掉的连接。**这种失败浏览器会自己换条连接重发 GET,但绝不重发 POST**
 * (它不知道这个 POST 有没有副作用),所以全站只有这个查询接口露了馅。
 *
 * 我们知道它没有副作用:只读查询、同参数当日缓存,重发一次是安全的。
 * **收藏的增删是真写入,不在这里重试** —— 那种要是重发,就是多一条收藏。
 * HTTP 错误(ApiError)一律不重试:服务器已经答过了,再问一次还是同一个答案。
 */
async function runQuery() {
  if (!canQuery.value) return
  querying.value = true
  errorMessage.value = ''
  try {
    let envelope
    try {
      envelope = await queryOnce()
    } catch (error) {
      if (!isNetworkFailure(error)) throw error
      envelope = await queryOnce()
    }
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
  favoriteName.value = `${legLabel('leg1')}-${legLabel('leg2')}`
  favoriteDialog.value = true
}

async function saveFavorite() {
  const name = favoriteName.value.trim()
  if (!name) return
  savingFavorite.value = true
  try {
    await createSpreadFavorite({
      name,
      provider: 'self',
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
      spread_provider_unavailable: '行情数据暂时读不到，请稍后再试',
      spread_provider_rate_limited: '数据源正在限频，请稍后再试',
      spread_provider_forbidden: '数据源当前拒绝访问',
      // 自研引擎下这条的含义变了：不是上游改了格式，而是这两条腿凑不出完整窗口。
      spread_provider_contract_changed: '这两条腿的历史数据不足以切出完整的年度窗口',
      provider_selection_invalid: '这个品种我们自己还没有行情数据',
      invalid_leg_selection: '同一条合约不能减自己：同品种时两腿月份要不同',
      favorite_exists: '该组合已在收藏中'
    }
    return known[error.code] ?? `请求失败（${error.code}）`
  }
  // 原本直接印 error.message,于是运营者看到的是一句生英文 `Failed to fetch`——
  // 它既没说清是「没送到」还是「服务器出错」,也没说该怎么办。
  if (isNetworkFailure(error)) return `${failureHint(error)}已自动重发过一次仍未成功，请再点一次「重试当前组合」。`
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

function downloadCsv() {
  const current = result.value
  if (!current) return
  const rows = current.continuous_series.points.map((point) => [
    point.trade_date,
    point.value,
    point.from_code.toUpperCase(),
    point.to_code.toUpperCase(),
    point.segment_no
  ].join(','))
  // Prefixed with a BOM so Excel opens the UTF-8 header without mojibake.
  const csv = `﻿交易日,价差,第一腿,第二腿,段\n${rows.join('\n')}\n`
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `自由价差-${subtitle.value.replace(/[^\w一-龥-]+/g, '_')}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

/** 深链接：套利监控点「看价差走势」过来时，直接把那一组合约填好并查出来。
 *
 * 只跳转不填参数等于把人扔在一个空表单前，还得自己回想刚才看的是哪两条腿。
 * 参数用**品种代码**（AP/FG）而不是中文名：调用方拿到的是代码，而中文名在
 * product_instrument_scope 里是可改的，拿会变的东西做链接参数迟早对不上。
 */
async function applyDeepLink(query: Record<string, unknown>) {
  const pick = (key: string) => {
    const value = query[key]
    return typeof value === 'string' && value.trim() ? value.trim().toUpperCase() : ''
  }
  const wanted: Array<[LegKey, string, string]> = [
    ['leg1', pick('symbol1'), pick('month1')],
    ['leg2', pick('symbol2'), pick('month2')]
  ]
  if (wanted.some(([, symbol, month]) => !symbol || !month)) return
  for (const [key, symbol, month] of wanted) {
    const item = varieties.value.find((v) => v.symbol.toUpperCase() === symbol)
    if (!item) return
    legs[key].variety = item.name
    await changeVariety(key, month)
    // 月份对不上就停手，不要拿默认月份查一组人家没点的合约——
    // 图会正常画出来，而画的根本不是他想看的那一对。
    if (legs[key].month !== month) return
  }
  await runQuery()
}

onMounted(async () => {
  loadingVarieties.value = true
  errorMessage.value = ''
  try {
    const [varietyEnvelope] = await Promise.all([getSpreadVarieties(), loadFavorites()])
    varieties.value = varietyEnvelope.data.items
    source.value = varietyEnvelope.data.source
  } catch (error) {
    errorMessage.value = describeError(error)
  } finally {
    loadingVarieties.value = false
  }
  // 组件在单元测试里是不挂路由直接 mount 的，那时 useRoute() 是 undefined。
  // 深链接只是增强，没有路由就没有深链接——不该因此把整个页面的挂载搞崩。
  await applyDeepLink(route?.query ?? {})
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
        <span v-if="favorites.length" class="preset-label">收藏</span>
        <el-tag v-for="favorite in favorites" :key="favorite.id" closable round class="favorite-pill"
          @click="applyFavorite(favorite)" @close.stop="removeFavorite(favorite)">
          {{ favorite.name }}
        </el-tag>
        <el-button text :icon="Plus" @click="openFavoriteDialog">自定义收藏</el-button>
      </div>
    </section>

    <!-- 跨品种价差(DEC-161):引擎照算两腿收盘价之差,但**可比与否要人来判断** ——
         玻纯同为元/吨、点值同 20 所以相减有意义;拿 IH(指数点·300)去减 FG 就没有。 -->
    <el-alert
      v-if="crossVariety"
      class="quality-alert"
      type="info"
      :closable="false"
      show-icon
      title="跨品种价差:两腿报价单位需可比"
      description="引擎只做「前腿收盘价 − 后腿收盘价」。玻璃与纯碱同为元/吨、点值同为 20,相减有意义;若两腿单位或点值不同(如指数点 vs 元/吨),得到的数没有交易含义。"
    />
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
            <span>上游数据止于：{{ result.source.data_cutoff_at ?? '—' }}</span>
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
          末点 {{ lastPoint?.trade_date ?? '—' }} · 本段可交易窗口截止 {{ activeWindowEnd ?? '—' }} ·
          窗口算法 {{ result.algorithm_versions.window }} · 规则 {{ result.algorithm_versions.rule }}
        </div>
        <el-collapse v-if="result.continuous_series.points.length" class="data-view">
          <el-collapse-item name="data-view">
            <template #title>
              <span class="data-view-title">数据视图（{{ result.continuous_series.points.length }} 个可交易日）</span>
            </template>
            <div class="data-view-actions">
              <el-button size="small" :icon="Download" @click="downloadCsv">导出 CSV</el-button>
            </div>
            <el-table :data="result.continuous_series.points" height="320" size="small" stripe>
              <el-table-column prop="trade_date" label="交易日" width="130" />
              <el-table-column label="价差" width="120" align="right">
                <template #default="{ row }">{{ row.value }}</template>
              </el-table-column>
              <el-table-column label="合约对" min-width="180">
                <template #default="{ row }">
                  {{ row.from_code.toUpperCase() }} − {{ row.to_code.toUpperCase() }}
                </template>
              </el-table-column>
              <el-table-column prop="segment_no" label="段" width="80" align="right" />
            </el-table>
          </el-collapse-item>
        </el-collapse>
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
      <span><el-icon><Star /></el-icon> 数据来源：{{ source?.source_display_name ?? '自建价差引擎' }}</span>
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
.free-spread-page { max-width: 1480px; margin: 0 auto; color: var(--tv-text); }
.spread-breadcrumb { margin: 2px 0 18px; color: var(--tv-text-secondary); font-size: 15px; }
.spread-breadcrumb span { margin: 0 8px; color: var(--tv-text-muted); }
.spread-card { background: var(--tv-bg-card); border: 1px solid var(--tv-border); border-radius: 20px; box-shadow: var(--tv-shadow); }
.controls-card { padding: 28px 30px 23px; margin-bottom: 18px; }
.selector-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.variety-select { width: 230px; }
.month-select { width: 150px; }
.spread-minus { color: var(--tv-text-secondary); font-size: 26px; }
.query-button { min-width: 100px; height: 42px; }
.preset-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.preset-label { color: var(--tv-text-secondary); margin-right: 2px; }
.favorite-pill { cursor: pointer; height: 32px; padding: 0 12px; }
.quality-alert { margin: 0 0 18px; }
.chart-card, .analytics-card { padding: 28px 30px 22px; margin-top: 18px; }
.section-heading { display: flex; justify-content: space-between; gap: 28px; align-items: flex-start; }
.section-heading h1, .section-heading h2 { margin: 0; color: var(--tv-text); }
.section-heading h1 { font-size: 25px; }
.section-heading h2 { font-size: 23px; }
.section-heading p { margin: 8px 0 0; color: var(--tv-text-secondary); font-size: 14px; }
.section-heading.compact { margin-bottom: 2px; }
.heading-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; color: var(--tv-text-secondary); font-size: 13px; white-space: nowrap; }
.heading-meta strong { color: var(--tv-text-secondary); font-size: 16px; font-weight: 500; }
.chart-wrap { margin-top: 10px; }
/* Kept in flow below the chart: floating them over the bottom-right corner
   covered the last date labels and the zoom slider. */
.export-actions { display: flex; justify-content: flex-end; gap: 4px; margin-top: 6px; }
.chart-footnote { margin-top: 12px; color: var(--tv-text-secondary); font-size: 13px; }
.matrix-heading { align-items: center; }
.matrix-legend { color: var(--tv-text-secondary); white-space: nowrap; }
.up-dot, .down-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 5px 0 12px; }
.up-dot { background: var(--tv-up); }
.down-dot { background: var(--tv-down); }
.matrix-scroll { overflow-x: auto; margin-top: 18px; }
.monthly-matrix { width: 100%; min-width: 1080px; border-collapse: separate; border-spacing: 0; text-align: center; font-variant-numeric: tabular-nums; }
.monthly-matrix th, .monthly-matrix td { padding: 13px 9px; border-bottom: 1px solid var(--tv-border); }
.monthly-matrix thead th { color: var(--tv-text-secondary); font-weight: 500; }
.monthly-matrix tbody th { text-align: left; color: var(--tv-text); font-weight: 600; white-space: nowrap; }
.monthly-matrix td { color: var(--tv-text); }
.monthly-matrix .matrix-up { background: var(--tv-up-bg); color: var(--tv-up); }
.monthly-matrix .matrix-down { background: var(--tv-down-bg); color: var(--tv-down); }
.monthly-matrix .matrix-flat { background: var(--tv-bg-inset); }
.monthly-matrix .matrix-empty { color: var(--tv-text-muted); background: var(--tv-bg-card); }
.monthly-matrix sup { margin-left: 2px; color: var(--tv-warn); }
.ratio-row td, .ratio-row th { border-bottom: 0; font-weight: 600; background: var(--tv-bg-card); }
.initial-empty { margin-top: 18px; min-height: 310px; display: grid; place-items: center; }
.source-footer { display: flex; justify-content: space-between; gap: 18px; padding: 18px 6px 4px; color: var(--tv-text-secondary); font-size: 13px; }
.source-footer span { display: flex; align-items: center; gap: 5px; }
@media (max-width: 980px) {
  .section-heading { flex-direction: column; }
  .heading-meta { align-items: flex-start; white-space: normal; }
  .source-footer { flex-direction: column; }
}

.data-view {
  margin-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.data-view-title {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.data-view-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}
</style>
