<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { EChartsOption, CandlestickSeriesOption } from 'echarts'
import {
  createSeatFavorite,
  deleteSeatFavorite,
  getSeatFavorites,
  getSeatNetPosition,
  getSpreadVarieties,
  type NetPositionDay,
  type SeatFavorite,
  type SeatNetPositionResponse
} from '../api'
import SpreadChart from '../components/SpreadChart.vue'
import { chartTokens, sliderStyle, tooltipStyle } from '../chartTheme'
import { offBoardBands } from '../offBoard'
import { searchHit } from '../pinyin'
import { lastYearStartIndex } from '../spreadCharts'
import { useAuthStore } from '../stores/auth'

/** 一次最多合并多少家。与后端的 MAX_NET_POSITION_MEMBERS 是同一个数。 */
const MAX_MEMBERS = 10

// 选择记在本地，与席位页各记各的：那边看一家，这边看一组，互相覆盖只会添乱。
const STORE_KEYS = {
  instrument: 'netPosition.instrument',
  contract: 'netPosition.contract',
  members: 'netPosition.members'
} as const

function remembered(key: keyof typeof STORE_KEYS) {
  try {
    return localStorage.getItem(STORE_KEYS[key]) ?? ''
  } catch {
    // 隐私模式下 localStorage 会抛异常。记不住是小事，页面打不开是大事。
    return ''
  }
}

function remember(key: keyof typeof STORE_KEYS, value: string) {
  try {
    localStorage.setItem(STORE_KEYS[key], value)
  } catch {
    // 同上：存不住就算了，不影响用。
  }
}

const auth = useAuthStore()

const instrument = ref(remembered('instrument'))
const contract = ref(remembered('contract'))
// 读回来要去重。本地存的值是能被手工改的，不能假定它干净：同一家进来两次会让
// 「最多十家」的计数虚高，也会和后端去重后的回显对不上——后端那边挡住了合计翻倍，
// 但界面上会看到自己选了 6 家而下拉里只勾着 5 家。
const members = ref<string[]>([
  ...new Set(
    remembered('members')
      .split(',')
      .filter((name) => name.length > 0)
  )
])
const allMembers = ref<string[]>([])
const contracts = ref<string[]>([])
const days = ref<NetPositionDay[]>([])
const priceSeriesKind = ref<SeatNetPositionResponse['price_series_kind']>(null)
const loading = ref(false)

const varietyNames = ref<Record<string, string>>({})
const varieties = ref<string[]>([])
function varietyLabel(code: string) {
  const name = varietyNames.value[code]
  return name && name !== code ? `${name} ${code}` : code
}

const favorites = ref<SeatFavorite[]>([])
const favoriteName = ref('')
const savingFavorite = ref(false)

// —— 两个搜索框 ——
//
// 用自定义过滤而不是 el-select 自带的 filterable：自带的那个是大小写敏感的
// 字面包含，输入小写 au 找不到「黄金 AU」，更别说用拼音首字母找「高盛」。
// filter-method 只负责把输入记下来，真正的过滤在下面两个 computed 里。
const varietyQuery = ref('')
const memberQuery = ref('')

const filteredVarieties = computed(() =>
  varieties.value.filter((code) => searchHit(varietyLabel(code), varietyQuery.value))
)
const filteredMembers = computed(() =>
  allMembers.value.filter((name) => searchHit(name, memberQuery.value))
)

watch(instrument, (value) => {
  if (value) remember('instrument', value)
})
// 合约连空值一起记：空就是「合约汇总」这个选择本身。
watch(contract, (value) => remember('contract', value))
watch(members, (value) => remember('members', value.join(',')), { deep: true })

async function loadVarieties() {
  try {
    const { data } = await getSpreadVarieties('self')
    varietyNames.value = Object.fromEntries(data.items.map((item) => [item.symbol, item.name]))
    varieties.value = data.items.map((item) => item.symbol).sort()
  } catch {
    // 只是选项和显示名。取不到就让人自己看代码，不打断这个页面。
  }
}

async function load() {
  if (!instrument.value) {
    days.value = []
    allMembers.value = []
    contracts.value = []
    return
  }
  loading.value = true
  try {
    const { data } = await getSeatNetPosition({
      instrument: instrument.value,
      members: members.value,
      contract: contract.value || undefined
    })
    days.value = data.days
    allMembers.value = data.all_members
    contracts.value = data.contracts
    priceSeriesKind.value = data.price_series_kind
    // 记住的合约可能已经到期，或者本就不属于这个品种。不在选项里就退回合约汇总，
    // 留在那里只会显示一张空表，看上去像数据坏了。
    if (contract.value && !data.contracts.includes(contract.value)) {
      contract.value = ''
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '净持仓读取失败')
    days.value = []
  } finally {
    loading.value = false
  }
}

async function loadFavorites() {
  try {
    const { data } = await getSeatFavorites()
    favorites.value = data
  } catch {
    // 收藏读不到不影响看数据，页面照常用。
  }
}

onMounted(() => {
  loadVarieties()
  loadFavorites()
  load()
})
watch([instrument, contract], () => load())
watch(members, () => load(), { deep: true })

// —— 收藏 ——

/**
 * 取写入保护令牌。
 *
 * store 里这个令牌是**懒加载**的：刚进页面或刷新之后它是 null，要先 loadCsrf()。
 * 直接拿 `csrfToken ?? ''` 送出去，后端会以 403「request is not allowed」拒掉，
 * 而错误信息里看不出缺的是令牌——2026-08-15 就是这么坏的。
 */
async function csrf() {
  if (!auth.csrfToken) await auth.loadCsrf()
  if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
  return auth.csrfToken
}

async function saveFavorite() {
  const name = favoriteName.value.trim()
  if (!name) {
    ElMessage.warning('给这组席位起个名字')
    return
  }
  if (!members.value.length) {
    ElMessage.warning('先选几个席位')
    return
  }
  savingFavorite.value = true
  try {
    const { data } = await createSeatFavorite({ name, members: members.value }, await csrf())
    favorites.value = [data, ...favorites.value]
    favoriteName.value = ''
    ElMessage.success(`已收藏「${data.name}」`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '收藏失败')
  } finally {
    savingFavorite.value = false
  }
}

function applyFavorite(favorite: SeatFavorite) {
  // 整组替换，不是并集：他点的是「换成这一组」。并进去会悄悄超过十家上限，
  // 而且他分不清哪几家是刚点进来的。
  members.value = [...favorite.members]
}

async function removeFavorite(favorite: SeatFavorite) {
  try {
    await ElMessageBox.confirm(`删掉收藏「${favorite.name}」？`, '确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
  } catch {
    return // 点了取消
  }
  try {
    await deleteSeatFavorite(favorite.id, await csrf())
    favorites.value = favorites.value.filter((item) => item.id !== favorite.id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

// —— 图 ——

const num = (value: string | null) => (value === null || value === '' ? null : Number(value))
const dates = computed(() => days.value.map((day) => day.trade_date))
const netSeries = computed(() => days.value.map((day) => Number(day.net_position)))
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

/**
 * 有席位掉榜的那几段，在两张图上都标出底色。
 *
 * 这段时间的合计**少算了几家**——不标出来的话，曲线上那道台阶看起来就是减仓。
 */
const bands = computed(() =>
  offBoardBands(
    days.value.map((day) => ({
      trade_date: day.trade_date,
      known: day.missing_members.length === 0
    }))
  )
)
const incompleteMark = computed(() => ({
  silent: true,
  // 掉榜底色：强调色加 16% 透明度（0x29），跟随主题。
  itemStyle: { color: `${chartTokens().accent}29` },
  label: { show: false },
  data: bands.value
}))
const incompleteDays = computed(
  () => days.value.filter((day) => day.missing_members.length > 0).length
)

const lots = (value: number) => `${value.toLocaleString('zh-CN')} 手`
const price = (value: number) => value.toFixed(2)

function row(label: string, value: string, color?: string) {
  const painted = color ? `<span style="color:${color};font-weight:600">${value}</span>` : value
  return `<div style="display:flex;gap:12px;justify-content:space-between"><span>${label}</span>${painted}</div>`
}

function tooltipBody(index: number, head: string[] = []) {
  const day = days.value[index]
  if (!day) return ''
  const tokens = chartTokens()
  const parts = [`<div style="margin-bottom:4px"><b>${day.trade_date}</b></div>`, ...head]

  const long = Number(day.long_lots)
  const short = Number(day.short_lots)
  if (long > 0) parts.push(row('多单', lots(long), tokens.up))
  if (short > 0) parts.push(row('空单', lots(short), tokens.down))

  const net = Number(day.net_position)
  parts.push(
    row(
      '合计净持仓',
      lots(Math.abs(net)) + (net === 0 ? '' : net > 0 ? '（净多）' : '（净空）'),
      net === 0 ? undefined : net > 0 ? tokens.up : tokens.down
    )
  )
  parts.push(row('计入席位', `${day.counted_members.length} 家`))
  // 掉榜必须逐个点名。只说「少了一家」，看的人不知道少的是不是他最在意的那家。
  if (day.inferred_members.length) {
    parts.push(
      row(
        '按反推计入',
        `<span style="color:${tokens.accent}">${day.inferred_members.join('、')}（实际未上榜,数字由回榜日倒推）</span>`
      )
    )
  }
  if (day.missing_members.length) {
    parts.push(
      row(
        '当日掉榜',
        `<span style="color:${tokens.accent}">${day.missing_members.join('、')}（未计入）</span>`
      )
    )
  }
  return parts.join('')
}

function axisIndex(params: unknown) {
  const first = Array.isArray(params) ? params[0] : params
  const index = (first as { dataIndex?: number } | undefined)?.dataIndex
  return typeof index === 'number' ? index : null
}

const tooltip = {
  trigger: 'axis' as const,
  formatter: (params: unknown) => {
    const index = axisIndex(params)
    return index === null ? '' : tooltipBody(index)
  }
}

const zoom = computed(() => [
  {
    type: 'inside' as const,
    zoomOnMouseWheel: false,
    moveOnMouseWheel: false,
    startValue: lastYearStartIndex(dates.value),
    endValue: Math.max(dates.value.length - 1, 0)
  },
  {
    type: 'slider' as const,
    startValue: lastYearStartIndex(dates.value),
    endValue: Math.max(dates.value.length - 1, 0),
    height: 26,
    bottom: 8,
    ...sliderStyle(),
    labelFormatter: (value: number) => dates.value[Math.round(value)] ?? ''
  }
])
const GRID_BOTTOM = 62

const priceOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 60, right: 24, top: 24, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    tooltip: {
      ...tooltip,
      ...tooltipStyle(),
      formatter: (params: unknown) => {
        const index = axisIndex(params)
        if (index === null) return ''
        // ECharts 的 K 线原样是 [开, 收, 低, 高]，别按图上的高低顺序读。
        const bar = candles.value[index]
        const head = Array.isArray(bar)
          ? [
              row('开盘', price(bar[0])),
              row('收盘', price(bar[1])),
              row('最低', price(bar[2])),
              row('最高', price(bar[3]))
            ]
          : [row('行情', '当日无 K 线')]
        return tooltipBody(index, head)
      }
    },
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    yAxis: {
      type: 'value' as const,
      scale: true,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: [
      {
        name: 'K线',
        type: 'candlestick' as const,
        // 红涨绿跌（国内惯例）：阳线 up、阴线 down。
        itemStyle: {
          color: tokens.up,
          borderColor: tokens.up,
          color0: tokens.down,
          borderColor0: tokens.down
        },
        data: candles.value as unknown as CandlestickSeriesOption['data'],
        markArea: incompleteMark.value
      }
    ]
  }
})

const netOption = computed<EChartsOption>(() => {
  const tokens = chartTokens()
  return {
    grid: { left: 72, right: 24, top: 16, bottom: GRID_BOTTOM },
    dataZoom: zoom.value,
    tooltip: { ...tooltip, ...tooltipStyle() },
    xAxis: {
      type: 'category' as const,
      data: dates.value,
      axisLabel: { hideOverlap: true, color: tokens.axisLabel },
      axisLine: { lineStyle: { color: tokens.axisLine } }
    },
    // scale 不能开：净持仓要看得出离零轴多远，多空翻向也全靠零轴分界。
    yAxis: {
      type: 'value' as const,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: [
      {
        name: '合计净持仓',
        type: 'line' as const,
        showSymbol: false,
        data: netSeries.value,
        lineStyle: { color: tokens.up, width: 2 },
        itemStyle: { color: tokens.up },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: tokens.baseline, type: 'dashed' as const },
          data: [{ yAxis: 0 }],
          label: { show: false }
        },
        markArea: incompleteMark.value
      }
    ]
  }
})

/** 最新一天的摘要，常驻在标题旁——不必悬停就知道现在什么情况。 */
const latest = computed(() => {
  const day = days.value[days.value.length - 1]
  if (!day) return null
  const net = Number(day.net_position)
  return {
    date: day.trade_date,
    net,
    counted: day.counted_members.length,
    missing: day.missing_members
  }
})

const priceSeriesNote = computed(() => {
  if (contract.value) return null
  switch (priceSeriesKind.value) {
    case 'open_interest_weighted':
      return '按持仓量加权 · 合成价'
    case 'dominant_unadjusted':
      return '主力连续 · 不复权，换月处有跳空'
    default:
      return null
  }
})
</script>

<template>
  <section class="page">
    <header class="page-head">
      <h1>净持仓</h1>
      <p class="lede">选几家席位一起看：把它们在这个品种上的持仓加到一起。</p>
    </header>

    <el-card shadow="never">
      <div class="control-row">
        <el-select
          v-model="instrument"
          style="width: 160px"
          placeholder="选择品种"
          filterable
          :filter-method="(query: string) => (varietyQuery = query)"
          :disabled="loading"
          @change="contract = ''"
          @visible-change="(visible: boolean) => { if (!visible) varietyQuery = '' }"
        >
          <el-option
            v-for="code in filteredVarieties"
            :key="code"
            :label="varietyLabel(code)"
            :value="code"
          />
        </el-select>
        <el-select
          v-model="contract"
          style="width: 220px"
          placeholder="合约汇总（全部合约）"
          :disabled="loading || !instrument"
        >
          <el-option label="合约汇总（全部合约）" value="" />
          <el-option v-for="code in contracts" :key="code" :label="code" :value="code" />
        </el-select>
        <el-select
          v-model="members"
          multiple
          filterable
          :filter-method="(query: string) => (memberQuery = query)"
          :multiple-limit="MAX_MEMBERS"
          @visible-change="(visible: boolean) => { if (!visible) memberQuery = '' }"
          style="min-width: 320px; flex: 1"
          :placeholder="instrument ? `选席位（最多 ${MAX_MEMBERS} 家）` : '先选品种'"
          :disabled="loading || !instrument"
        >
          <el-option v-for="name in filteredMembers" :key="name" :label="name" :value="name" />
        </el-select>
      </div>

      <!-- 收藏区：选一组席位是件反复做的事，存下来一点就回填。 -->
      <div class="favorites">
        <span class="favorites-label">收藏</span>
        <el-tag
          v-for="favorite in favorites"
          :key="favorite.id"
          class="favorite-tag"
          closable
          :title="favorite.members.join('、')"
          @click="applyFavorite(favorite)"
          @close="removeFavorite(favorite)"
        >
          {{ favorite.name }}（{{ favorite.members.length }}）
        </el-tag>
        <span v-if="!favorites.length" class="muted">还没有收藏。选好几家后在右边起个名存下来。</span>
        <span class="favorites-save">
          <el-input
            v-model="favoriteName"
            size="small"
            style="width: 150px"
            placeholder="给这组起个名"
            :disabled="!members.length"
            @keyup.enter="saveFavorite"
          />
          <el-button
            size="small"
            :loading="savingFavorite"
            :disabled="!members.length || !favoriteName.trim()"
            @click="saveFavorite"
          >
            收藏当前 {{ members.length }} 家
          </el-button>
        </span>
      </div>

      <p class="note">
        合计把每家席位在<b>各个合约上的持仓逐一算完再相加</b>：净多的那些合成「多单」、净空的合成「空单」，
        两者相减才是合计净持仓。<b>不算成本、不算盈亏</b>——几家机构的仓不是同一笔仓，
        给它们算一个平均成本会得出一个不对应任何真实仓位的数。
      </p>
      <p class="note">
        某家掉出交易所前二十时，那天他的持仓是<b>未知</b>，<b>不计入</b>合计，图上以底色标出、小窗里点名。
        按零计会画出一根假的大幅减仓。
      </p>
    </el-card>

    <el-empty
      v-if="!loading && !instrument"
      description="选一个品种开始"
    />
    <el-empty
      v-else-if="!loading && !members.length"
      description="选几家席位（最多 10 家），它们的持仓会加到一起"
    />
    <el-empty v-else-if="!loading && !days.length" description="这几家席位在该品种上没有持仓记录" />
    <template v-else>
      <el-card shadow="never">
        <template #header>
          <div class="panel-head">
            <h2>行情</h2>
            <span v-if="priceSeriesNote" class="series-note">{{ priceSeriesNote }}</span>
          </div>
        </template>
        <SpreadChart v-if="hasCandles" :option="priceOption" :height="300" group="net-position" export-name="净持仓-行情" />
        <el-alert
          v-else
          type="info"
          :closable="false"
          title="这段时间没有行情"
          description="该品种在这些交易日上没有可用的开高低收，K 线画不出来。下面的净持仓不受影响。"
        />
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="panel-head">
            <h2>合计净持仓</h2>
            <div v-if="latest" class="latest">
              <span class="latest-date">{{ latest.date }}</span>
              <span :class="latest.net === 0 ? '' : latest.net > 0 ? 'up' : 'down'">
                {{ Math.abs(latest.net).toLocaleString('zh-CN') }} 手{{
                  latest.net === 0 ? '' : latest.net > 0 ? '（净多）' : '（净空）'
                }}
              </span>
              <span>计入 {{ latest.counted }} 家</span>
              <span v-if="latest.missing.length" class="warn">
                {{ latest.missing.join('、') }} 当日掉榜，未计入
              </span>
            </div>
          </div>
        </template>
        <SpreadChart :option="netOption" :height="320" group="net-position" export-name="净持仓-合计" />
        <p v-if="incompleteDays" class="note warn">
          这段区间里有 {{ incompleteDays }} 天至少有一家掉出前二十，合计少算了那几家（图上底色标出）。
        </p>
      </el-card>
    </template>
  </section>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head h1 {
  margin: 0 0 4px;
}
.lede {
  margin: 0;
  color: var(--tv-text-secondary);
}
.control-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.favorites {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--tv-border);
}
.favorites-label {
  font-weight: 600;
  color: var(--tv-text);
}
.favorite-tag {
  cursor: pointer;
}
.favorites-save {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}
.note {
  margin: 12px 0 0;
  color: var(--tv-text-secondary);
  line-height: 1.7;
}
.muted {
  color: var(--tv-text-muted);
}
.warn {
  color: var(--tv-warn);
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.panel-head h2 {
  margin: 0;
}
.series-note {
  font-size: 13px;
  color: var(--tv-text-muted);
}
.latest {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--tv-text-secondary);
}
.latest-date {
  font-weight: 600;
  color: var(--tv-text);
}
.latest .up {
  color: var(--tv-up);
  font-weight: 600;
}
.latest .down {
  color: var(--tv-down);
  font-weight: 600;
}
</style>
