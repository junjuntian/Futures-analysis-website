<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  getSpreadMonitor,
  getSpreadVarieties,
  saveSpreadTemplateNote,
  type SpreadMonitorItem,
  type SpreadMonitorTrack
} from '../api'
import { driftTone, isChoppy, isDecayZone, isQualified, isRedLine, points, revertPct, revertTone } from '../revert'
import { useAuthStore } from '../stores/auth'

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
// 只看刚进极值的。焦煤 2026 年有 64% 的交易日都在 3% 触发（价差持续创新低，滚动
// 区间天天被刷新），而连续触发段的中位长度只有 3 日——长段拖着不放才是噪音的来源。
const onlyNew = ref(false)
// 只看今天的进场信号:每天盘后勾上它,列表要么是空的,要么就是今天该动手的单子。
const onlyEntry = ref(false)
// 历史信号(DEC-070):一次取回全部快照日,只列历来的 ⚡ 进场行——
// 运营者要回看信号不必逐个日期点选。
const historyMode = ref(false)

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
    const { data } = await getSpreadMonitor(
      threshold.value,
      tradeDate.value || undefined,
      historyMode.value
    )
    items.value = data.items
    asOf.value = data.as_of
    availableDates.value = data.available_dates
    if (!historyMode.value && !tradeDate.value && data.as_of) tradeDate.value = data.as_of
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
watch([threshold, tradeDate, historyMode], () => void load())

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
      if (direction.value !== 'all' && (item.alert ?? item.turn) !== direction.value) return false
      // 「仅新触发」只筛触发中那一组；已拐头与未触发的行没有新旧之分，
      // 让它们跟着一起消失会让人以为下面那半屏也被过滤了。
      if (onlyNew.value && item.alert && !item.is_new_alert) return false
      if (onlyEntry.value && !isEntry(item)) return false
      return true
    })
    // 干净的进场信号最上,信号差的进场其次,其余按离中线远近排(运营者要求)。
    .sort((a, b) => entryRank(b) - entryRank(a) || extremity(b) - extremity(a))
})

/** ⚡ 进场 = 今天刚拐头 × 本轮首次穿线 × 资格合格 × 未进交割红线。
 * 红线内不是没机会,是《体系》的硬纪律+数据实证的负期望区,不当进场信号。
 * 「首次」用 turn_crosses===1 判(DEC-070,运营者拍板):20 日窗内的第二次及
 * 以后穿线只挂 ⚠ 信号差,不再亮 ⚡;窗滑过之后的新穿线计数归 1,算新一轮。 */
function isEntry(item: SpreadMonitorItem) {
  return (
    item.is_new_turn &&
    item.turn_crosses === 1 &&
    item.revert !== null &&
    isQualified(item.revert) &&
    !isRedLine(item.days_left)
  )
}
/** 排序权重:干净进场 2 > 信号差进场 1 > 其余 0。 */
function entryRank(item: SpreadMonitorItem) {
  if (!isEntry(item)) return 0
  return isChoppy(item.turn_crosses) ? 1 : 2
}

// 「触发中」与「已拐头」同列展示:拐头行多半已退出报警带,只按 alert 分组的话,
// 恰恰在该进场的时候它掉进「未触发」堆里,规则第二层就白做了。
// 历史模式下 fired = 历来的进场行(新日期在前),quiet 不展示。
const fired = computed(() =>
  historyMode.value
    ? filtered.value
        .filter((item) => isEntry(item))
        .sort((a, b) => b.trade_date.localeCompare(a.trade_date))
    : filtered.value.filter((item) => item.alert || item.turn)
)
const quiet = computed(() =>
  historyMode.value ? [] : filtered.value.filter((item) => !item.alert && !item.turn)
)
const historyDays = computed(() => new Set(fired.value.map((item) => item.trade_date)).size)
const highCount = computed(() => fired.value.filter((item) => item.alert === 'high').length)
const lowCount = computed(() => fired.value.filter((item) => item.alert === 'low').length)
const newCount = computed(() => fired.value.filter((item) => item.is_new_alert).length)
const turnCount = computed(() => fired.value.filter((item) => item.turn).length)
const qualifiedCount = computed(
  () => fired.value.filter((item) => item.revert && isQualified(item.revert)).length
)
const entryCount = computed(() => fired.value.filter((item) => isEntry(item)).length)

const CHOPPY_HINT =
  '近 20 个交易日内穿线 2 次及以上 —— 拐头反复。JM 09-01 实例:八天三次穿线,' +
  '期间价差打回区间顶,前两次进场按「创报警后新高离场」都得止损。' +
  '次数越多信号越弱,降档仓位或放过。'

const REDLINE_HINT =
  '距可交易窗口止点 ≤15 个交易日(《体系》红线:交割前 15 个交易日全部清仓)。' +
  '留一法数据:合格段剩余 <15 日持到底中位 −21.7%,为正仅 20%。' +
  '红线内 ⚡ 进场信号被压制;已有持仓按纪律清仓。'
const DECAY_HINT =
  '距窗口止点 16~39 个交易日 —— 利润衰减区。留一法数据:合格段 15~40 日桶持到底' +
  '中位 −32.5%,>40 日 +54.8%。可做,但降档仓位,盈利主要来自剩余 40 天以上的机会。'

const ENTRY_HINT =
  '今天刚拐头(位置今天才穿过回撤线)且资格合格 —— 回放口径里的进场日。' +
  '统计是盘后算的,执行等于次日进场。同一轮拐头只亮首次:20 日内第二次及以后的' +
  '穿线不再给进场标,只挂 ⚠ 信号差(DEC-070,运营者拍板)。'

const TURN_HINT =
  '近 20 个交易日内当年轨曾进 3% 报警带，当前已自极值回撤超过该品种的档位' +
  '（全量回测分档:焦煤/苹果 20%、鸡蛋 5%、玻纯跨品种 8%、其余 10%——回撤线画在' +
  '位置刻度上,焦煤位置日抖动全场最高要深线,鸡蛋季节趋势强早进不受罚要浅线）。' +
  '分层规则的进场信号：报警只是机会出现，拐头才是上车点——全样本回放里报警当天' +
  '就进持到底中位为负，等拐头才转正。'
const QUALIFIED_HINT =
  '历年触及率 ≥80% 且「持到期」为正。留一法回放：合格的报警段持到底中位 +29% 区间，' +
  '不合格的 −26%。不合格的行没有徽标——没有徽标就是「别做」。'

const REVERT_HINT =
  '同月份组合（同品种 + 同月份对 + 同年差）跨年拼起来的样本，不是这一组合自己的胜率。' +
  '可交易窗口照 5A 那套：止点＝先到期那条腿的散户最后交易日。历年按月-日对齐，' +
  '从今天这个日历位置一直看到各自窗口的止点，期间任何一天触及即算回归（不比终点）。' +
  '只用已走完的年份。剩余期越长回归率越接近 100%，所以要连着「持到期」一起看：' +
  '它为负说明历年这段最终是朝反方向走的。'

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

// —— 手工产业备注(DEC-069):统计说「历年数字怎么说」,备注说「为什么」——
// 运营者从直播/盖楼学来的品种级知识(夏天的煤不能空、纯碱低库存是逼空窗口…)。
// 备注挂在**月份模板**上:JD2609−JD2701 和 JD2709−JD2801 共享同一条「09-01」。
const auth = useAuthStore()
const noteDialogOpen = ref(false)
const noteDraft = ref('')
const noteSaving = ref(false)
const noteTarget = ref<SpreadMonitorItem | null>(null)

function templateName(item: SpreadMonitorItem) {
  const m1 = item.contract_1.slice(-2)
  const m2 = item.contract_2.slice(-2)
  return item.is_cross_variety
    ? `${label(item.instrument_1)}${m1} − ${label(item.instrument_2)}${m2}`
    : `${label(item.instrument_1)} ${m1}-${m2}`
}

function openNoteEditor(item: SpreadMonitorItem) {
  noteTarget.value = item
  noteDraft.value = item.note ?? ''
  noteDialogOpen.value = true
}

async function saveNote() {
  const item = noteTarget.value
  if (!item) return
  noteSaving.value = true
  try {
    if (!auth.csrfToken) await auth.loadCsrf()
    if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
    const note = noteDraft.value.trim()
    await saveSpreadTemplateNote(
      {
        instrument_1: item.instrument_1,
        month_1: Number(item.contract_1.slice(-2)),
        instrument_2: item.instrument_2,
        month_2: Number(item.contract_2.slice(-2)),
        note
      },
      auth.csrfToken
    )
    // 同一模板可能对应表里好几行(JD 09-01 有多个年份对),本地全刷一遍,
    // 不用整页重查。
    const key = (x: SpreadMonitorItem) =>
      `${x.instrument_1}|${x.contract_1.slice(-2)}|${x.instrument_2}|${x.contract_2.slice(-2)}`
    const target = key(item)
    for (const row of items.value) {
      if (key(row) === target) row.note = note || null
    }
    noteDialogOpen.value = false
    ElMessage.success(note ? '备注已保存' : '备注已清空')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '备注保存失败')
  } finally {
    noteSaving.value = false
  }
}

// 带着这一组合约跳到自由价差页，那边会自动填好并查出来（见 applyDeepLink）。
// 传品种**代码**不传中文名：中文名在 product_instrument_scope 里可改，
// 拿会变的东西做链接参数迟早对不上。
function openDetail(item: SpreadMonitorItem) {
  void router.push({
    path: '/spread-analytics/free-spread',
    query: {
      symbol1: item.instrument_1,
      month1: item.contract_1.slice(-2),
      symbol2: item.instrument_2,
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
        盯住每一组合约的价差，看它是不是走到了历史极值。触发与否是按下面的阈值现算的，
        换个阈值同一天的结论就跟着变。
      </p>
      <p class="caveat">
        <strong>贴到极值不等于会回归。</strong>
        每组后面是它自己的历年成绩：从今天这个日历位置起，到该组合退出散户可交易区间为止，
        历年有几年曾经回落、最有利时能走多少点、一路持到最后又是多少。
        剩余时间越长「曾经回归」越容易达成，所以别只看那个百分比——
        <strong>持到期为负</strong>就说明历年这段最终是朝反方向走的。
        三步用法：只看带 <strong>✓ 合格</strong> 的行；<strong>⚡ 进场</strong> 亮的当晚
        就是信号日（次日执行），带它的行排在最上面；仓位按「风险预留」那个点数算:
        可承受亏损 ÷ (风险预留 × 点值) = 手数;浮亏到「补仓参考」是历年常态,不是逻辑坏了。
        <strong>剩余 ≤15 交易日进交割红线</strong>,⚡ 压制、持仓清掉;16~39 日是衰减区,降档。
        「已拐头」还挂着但 ⚡ 已灭的，是进场日已过的存量状态；
        带 <strong>⚠ 信号差</strong> 的是 20 日内反复拐头的组合——降档仓位或放过。
        没有徽标的报警，当风景。
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
          :disabled="historyMode || !availableDates.length"
        />

        <el-radio-group v-model="direction">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="high">高位</el-radio-button>
          <el-radio-button value="low">低位</el-radio-button>
        </el-radio-group>

        <el-checkbox v-model="onlyNew" border :disabled="historyMode">仅新触发</el-checkbox>
        <el-checkbox v-model="onlyEntry" border :disabled="historyMode">仅进场日</el-checkbox>
        <el-tooltip
          content="一次列出全部快照日的 ⚡ 进场信号(覆盖当前监控组合的整个可交易历史),不用逐个日期点选。已到期换掉的旧组合不在内。"
          placement="top"
        >
          <el-checkbox v-model="historyMode" border>历史信号</el-checkbox>
        </el-tooltip>
      </div>

      <div class="tally" v-if="!historyMode">
        <div class="cell"><span class="k">监控组合</span><span class="v">{{ items.length }}</span></div>
        <div class="cell"><span class="k">触发</span><span class="v">{{ fired.length }}</span></div>
        <div class="cell"><span class="k">新触发</span><span class="v fresh">{{ newCount }}</span></div>
        <div class="cell"><span class="k">已拐头</span><span class="v fresh">{{ turnCount }}</span></div>
        <div class="cell"><span class="k">✓ 合格</span><span class="v qual">{{ qualifiedCount }}</span></div>
        <div class="cell"><span class="k">⚡ 进场</span><span class="v entry">{{ entryCount }}</span></div>
        <div class="cell"><span class="k">高位</span><span class="v high">{{ highCount }}</span></div>
        <div class="cell"><span class="k">低位</span><span class="v low">{{ lowCount }}</span></div>
        <div class="cell" v-if="asOf"><span class="k">数据日</span><span class="v date">{{ asOf }}</span></div>
      </div>
      <div class="tally" v-else>
        <div class="cell"><span class="k">⚡ 历史进场信号</span><span class="v entry">{{ fired.length }}</span></div>
        <div class="cell"><span class="k">覆盖信号日</span><span class="v">{{ historyDays }}</span></div>
        <div class="cell" v-if="asOf"><span class="k">最新快照</span><span class="v date">{{ asOf }}</span></div>
      </div>
    </el-card>

    <el-empty v-if="!loading && !items.length" description="这一天还没有监控快照" />

    <template v-else>
      <h2 class="group-label">{{ historyMode ? '历史进场信号 · 新日期在前' : '触发中 · 已拐头' }}</h2>
      <el-empty
        v-if="!fired.length"
        :description="historyMode ? '已存快照日里没有出现过 ⚡ 进场信号' : '按当前阈值没有组合触发,也没有已拐头的组合'"
        :image-size="70"
      />
      <div class="rows" v-loading="loading">
        <article
          v-for="item in fired"
          :key="item.trade_date + item.contract_1 + item.contract_2"
          class="row"
          :class="(item.alert ?? item.turn) === 'high' ? 'fired-high' : 'fired-low'"
        >
          <div class="ident">
            <div class="pair">{{ comboName(item) }}</div>
            <div class="meta">
              <el-tag size="small" :type="item.is_cross_variety ? 'warning' : 'info'" effect="plain">
                {{ item.is_cross_variety ? '跨品种' : label(item.instrument_1) }}
              </el-tag>
              <el-tooltip
                v-if="item.is_new_alert"
                content="前一交易日按同一阈值还没触发，今天才进极值区。"
                placement="top"
              >
                <span class="badge-new">新</span>
              </el-tooltip>
              <span class="asof" v-if="historyMode || item.trade_date !== asOf">{{ item.trade_date }}</span>
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
            <div class="state-tags">
              <el-tooltip v-if="isRedLine(item.days_left)" :content="REDLINE_HINT" placement="top">
                <span class="badge-redline">临近交割 · 剩 {{ item.days_left }} 日</span>
              </el-tooltip>
              <el-tooltip
                v-else-if="isDecayZone(item.days_left)"
                :content="DECAY_HINT"
                placement="top"
              >
                <span class="badge-decay">衰减区 · 剩 {{ item.days_left }} 日</span>
              </el-tooltip>
              <el-tooltip v-if="isEntry(item)" :content="ENTRY_HINT" placement="top">
                <span class="badge-entry">⚡ 进场</span>
              </el-tooltip>
              <el-tag
                v-if="item.alert"
                :type="item.alert === 'high' ? 'danger' : 'success'"
                effect="light"
                size="small"
              >
                {{ item.alert === 'high' ? '高位' : '低位' }}
              </el-tag>
              <el-tooltip v-if="item.turn" :content="TURN_HINT" placement="top">
                <el-tag type="warning" effect="light" size="small">
                  <!-- 方向永远带上:LH2611−LH2705 出现过「高位报警 + 低位拐头」并存,
                       省掉后缀会让人把低位拐头读成高位的。 -->
                  已拐头{{ item.turn === 'high' ? ' · 高位' : ' · 低位' }}
                </el-tag>
              </el-tooltip>
              <el-tooltip v-if="isChoppy(item.turn_crosses)" :content="CHOPPY_HINT" placement="top">
                <span class="badge-choppy">⚠ 信号差 ×{{ item.turn_crosses }}</span>
              </el-tooltip>
              <el-tooltip
                v-if="item.revert && isQualified(item.revert)"
                :content="QUALIFIED_HINT"
                placement="top"
              >
                <span class="badge-q">✓ 合格</span>
              </el-tooltip>
            </div>

            <el-tooltip v-if="item.revert" :content="REVERT_HINT" placement="top">
              <div class="revert" :class="revertTone(item.revert)">
                <span class="rate">{{ revertPct(item.revert) }}</span>
                <span class="basis">
                  {{ item.revert.hit }}/{{ item.revert.n }} 年曾回归
                  <template v-if="item.revert.days">· 剩 {{ item.revert.days }} 天</template>
                </span>
                <span class="basis" v-if="points(item.revert.move_points)">
                  最有利 {{ points(item.revert.move_points) }}
                  <template v-if="points(item.revert.drift_points)">
                    · 持到期
                    <em :class="driftTone(item.revert.drift_points)">
                      {{ points(item.revert.drift_points) }}
                    </em>
                  </template>
                  点
                </span>
                <span class="basis mae" v-if="points(item.revert.mae_points)">
                  补仓参考 −{{ Math.abs(Number(item.revert.mae_points)).toFixed(0) }}
                  · 风险预留 −{{
                    item.revert.mae_max_points === null
                      ? '—'
                      : Math.abs(Number(item.revert.mae_max_points)).toFixed(0)
                  }}
                  点
                </span>
              </div>
            </el-tooltip>
            <div v-else class="revert absent">历年无可比样本</div>

            <div v-if="item.note" class="note" @click="openNoteEditor(item)">
              📝 {{ item.note }}
            </div>

            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
              <el-button v-if="!item.note" link size="small" @click="openNoteEditor(item)">备注</el-button>
            </div>
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
            <div v-if="item.note" class="note" @click="openNoteEditor(item)">
              📝 {{ item.note }}
            </div>
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(item)">看价差走势</el-button>
              <el-button v-if="!item.note" link size="small" @click="openNoteEditor(item)">备注</el-button>
            </div>
          </div>
        </article>
      </div>
    </template>

    <el-dialog
      v-model="noteDialogOpen"
      :title="noteTarget ? `产业备注 · ${templateName(noteTarget)}` : '产业备注'"
      width="480px"
    >
      <p class="note-hint">
        写给这个<strong>月份模板</strong>的品种知识,所有年份的同月组合共用一条
        (比如 JD 09-01 的备注,明年的 JD2709−JD2801 也会看到)。
        清空后保存即删除。
      </p>
      <el-input
        v-model="noteDraft"
        type="textarea"
        :rows="5"
        maxlength="2000"
        show-word-limit
        placeholder="例:夏天的煤不能做空(安监停产);12 月焦煤交割量大于 1 月"
      />
      <template #footer>
        <el-button @click="noteDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="noteSaving" @click="saveNote">保存</el-button>
      </template>
    </el-dialog>
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
  color: var(--tv-text-muted);
  font-weight: 650;
}
.tally .v {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.tally .v.high {
  color: var(--tv-up);
}
.tally .v.low {
  color: var(--tv-down);
}
.tally .v.fresh {
  color: var(--tv-warn);
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
  color: var(--tv-text-muted);
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
  background: var(--tv-bg-card);
  border: 1px solid var(--tv-border);
  border-radius: 8px;
}
.row.fired-high {
  border-left: 3px solid var(--tv-up);
}
.row.fired-low {
  border-left: 3px solid var(--tv-down);
}
.row.quiet {
  background: var(--tv-bg-inset);
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
  color: var(--tv-text-muted);
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
  color: var(--tv-text-muted);
  font-weight: 650;
}
.rail {
  position: relative;
  height: 22px;
  border-radius: 4px;
  background: var(--tv-bg-inset);
  border: 1px solid var(--tv-border);
}
.zone {
  position: absolute;
  top: 0;
  bottom: 0;
}
.zone.hi {
  right: 0;
  background: var(--tv-up-bg);
  border-radius: 0 3px 3px 0;
}
.zone.lo {
  left: 0;
  background: var(--tv-down-bg);
  border-radius: 3px 0 0 3px;
}
.mark {
  position: absolute;
  top: 50%;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
  border: 2px solid var(--tv-bg-card);
  background: var(--tv-text-muted);
  box-shadow: 0 0 0 1px var(--tv-border-strong);
}
.mark.high {
  background: var(--tv-up);
  box-shadow: 0 0 0 1px var(--tv-up);
}
.mark.low {
  background: var(--tv-down);
  box-shadow: 0 0 0 1px var(--tv-down);
}
.ends {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--tv-text-muted);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.absent {
  font-size: 11px;
  color: var(--tv-text-muted);
}
.pct {
  text-align: right;
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--tv-text-secondary);
}
.pct.high {
  color: var(--tv-up);
}
.pct.low {
  color: var(--tv-down);
}

.tail {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.row-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
/* 手工产业备注:统计旁边的「为什么」。整块可点进编辑。 */
.note {
  max-width: 300px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tv-text-secondary);
  background: color-mix(in srgb, var(--tv-warn) 8%, transparent);
  border-left: 2px solid var(--tv-warn);
  border-radius: 4px;
  padding: 4px 8px;
  text-align: left;
  white-space: pre-wrap;
  cursor: pointer;
}
.note:hover {
  background: color-mix(in srgb, var(--tv-warn) 14%, transparent);
}
.note-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--tv-text-secondary);
}

/* 页头的提醒段：贴到极值不等于会回归。 */
.caveat {
  color: var(--tv-text-secondary);
  border-left: 3px solid var(--tv-warn);
  padding-left: 12px;
  margin-top: 10px;
}
.caveat strong {
  color: var(--tv-text);
}

.state-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
/* 「✓ 合格」:分层规则第一层通过。用主色蓝,与涨跌红绿、警示橙都区分开——
   它是「值得做」,不是方向也不是警告。 */
.badge-q {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-blue);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-blue-bg);
  color: var(--tv-blue);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}
.tally .v.qual {
  color: var(--tv-blue);
}
.tally .v.entry {
  color: var(--tv-blue);
}
/* 「⚡ 进场」:全页唯一的实心蓝——它是唯一的行动指令,必须一眼跳出来。 */
.badge-entry {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: var(--tv-radius-sm);
  background: var(--tv-blue);
  color: var(--el-color-white);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}

/* 「临近交割」:灰底——不是机会也不是警报,是纪律性退出区。 */
.badge-redline {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-border-strong);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-bg-inset);
  color: var(--tv-text-muted);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}
.badge-decay {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px dashed var(--tv-border-strong);
  border-radius: var(--tv-radius-sm);
  color: var(--tv-text-muted);
  font-size: 11px;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}
.revert .basis.mae {
  color: var(--tv-text-secondary);
}

/* 「⚠ 信号差」:拐头反复。红色系但用描边不用实底——它是警示不是方向,
   也不能比 ⚡ 更抢眼。 */
.badge-choppy {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-up);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-up-bg);
  color: var(--tv-up);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  font-variant-numeric: tabular-nums;
}

/* 「新」徽标：前一交易日按同一阈值还没触发。
   用警示橙而不是红绿——红绿在全站是涨跌语义，借过来会被读成方向。 */
.badge-new {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border: 1px solid var(--tv-warn);
  border-radius: var(--tv-radius-sm);
  background: var(--tv-warn-bg);
  color: var(--tv-warn);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}

/* 历史回归率。同样避开红绿：这是个概率，不是价格方向。
   强弱只用主色与灰色的深浅区分。 */
.revert {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
  cursor: help;
}
.revert .rate {
  font-size: 17px;
  font-weight: 700;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
  color: var(--tv-text-secondary);
}
.revert.strong .rate {
  color: var(--tv-blue);
}
.revert.weak .rate {
  color: var(--tv-text-muted);
}
.revert .basis {
  font-size: 11px;
  line-height: 1.3;
  text-align: right;
  color: var(--tv-text-muted);
  font-variant-numeric: tabular-nums;
}
/* 「持到期」单独上色：它为负说明历年这段最终朝反方向走，是回归率骗人的地方。
   仍然避开红绿——这不是价格方向，是统计倾向。 */
.revert .basis em {
  font-style: normal;
  font-weight: 700;
}
.revert .basis em.with {
  color: var(--tv-blue);
}
.revert .basis em.against {
  color: var(--tv-warn);
}
.revert.absent {
  font-size: 11px;
  color: var(--tv-text-muted);
  cursor: default;
}

@media (max-width: 880px) {
  .row {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .tail {
    flex-direction: row;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 14px;
  }
  /* 窄屏下 .tail 横过来了，回归率跟着改成左对齐横排，
     否则右对齐的两行文字会吊在按钮中间。 */
  .revert {
    flex-direction: row;
    align-items: baseline;
    gap: 6px;
  }
  .revert .basis {
    text-align: left;
  }
}
</style>
