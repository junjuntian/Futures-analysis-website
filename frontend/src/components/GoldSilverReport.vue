<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getOverviewReport,
  getSeatPositions,
  saveOverviewReportLevels,
  saveOverviewReportSeatGroups,
  type OverviewReportResponse,
  type ReportLevelRow,
  type ReportSeatRow
} from '../api'
import { useAuthStore } from '../stores/auth'

// 「黄金白银报告表」。上下两半来源完全不同，读的时候一次拿回，写的时候分开写：
//   上半 压力位/支撑位 —— 运营者手填的盘面判断，平台无从计算，按交易日存。
//   下半 席位净持仓与筹码 —— 从事实表现算，筹码走的就是席位页那个成本引擎。
//
// 行结构照运营者那张复盘表原样搬，包括夹在压力位与支撑位中间的「星级评分度」
// 那一行——它那三格是文字不是数字，最后一列也是文字，别按其它行的样子渲染。

const auth = useAuthStore()

/** 上半部分的行。标签是结构，写死；格子里的内容才是数据。 */
const LEVEL_ROWS: Array<{ key: string; label: string; kind: 'level' | 'rating' }> = [
  { key: 'strong_resistance', label: '强压力位', kind: 'level' },
  { key: 'resistance', label: '压力位', kind: 'level' },
  { key: 'weak_resistance', label: '弱压力位', kind: 'level' },
  { key: 'rating', label: '星级评分度', kind: 'rating' },
  { key: 'weak_support', label: '弱支撑位', kind: 'level' },
  { key: 'support', label: '支撑位', kind: 'level' },
  { key: 'strong_support', label: '强支撑位', kind: 'level' }
]
const LEVEL_COLUMNS = ['外盘→现', '汇算→存', '内盘→沪']

const GROUP_LABELS: Record<string, string> = {
  institution: '机构席位（逐行显示，另出「机构持仓」合计）',
  watch: '其他关注（逐行显示，不进任何合计）',
  foreign: '外资席位（逐行显示，另出「外资持仓」合计）',
  retail: '散户席位（只出合计行）'
}

const report = ref<OverviewReportResponse | null>(null)
const loading = ref(true)
const editingLevels = ref(false)
const editingSeats = ref(false)
const saving = ref(false)
const draft = ref<ReportLevelRow[]>([])
const seatDraft = ref<Record<string, string[]>>({})
const allMembers = ref<string[]>([])

function blankRows(): ReportLevelRow[] {
  return LEVEL_ROWS.map((row) => ({
    key: row.key,
    values: ['', '', ''],
    bias: '',
    stars: row.kind === 'rating' ? null : 0,
    text: ''
  }))
}

/** 库里那份按 key 对齐到固定行上。缺的行补空——少一行就整张表错位。 */
function alignRows(stored: ReportLevelRow[] | undefined): ReportLevelRow[] {
  const byKey = new Map((stored ?? []).map((row) => [row.key, row]))
  return blankRows().map((blank) => {
    const found = byKey.get(blank.key)
    if (!found) return blank
    return {
      key: blank.key,
      values: [0, 1, 2].map((index) => found.values?.[index] ?? ''),
      bias: found.bias ?? '',
      stars: found.stars ?? blank.stars,
      text: found.text ?? ''
    }
  })
}

async function load() {
  loading.value = true
  try {
    const { data } = await getOverviewReport()
    report.value = data
    draft.value = alignRows(data.levels?.rows)
    seatDraft.value = Object.fromEntries(
      data.seat_groups.map((group) => [group.group_key, [...group.members]])
    )
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '报告表读取失败')
  } finally {
    loading.value = false
  }
}

/** 席位下拉的候选。取全量会员名录，与席位页同一份。 */
async function loadMembers() {
  try {
    const { data } = await getSeatPositions({})
    allMembers.value = data.members
  } catch {
    // 只是个候选列表。取不到就让他手敲，不打断整张报告表。
  }
}

onMounted(() => {
  load()
  loadMembers()
})

/** 「8.14 黄金白银报告表」。日期跟着数据走，不写死。 */
const title = computed(() => {
  const date = report.value?.trade_date
  if (!date) return '黄金白银报告表'
  const [, month, day] = date.split('-')
  return `${Number(month)}.${Number(day)} 黄金白银报告表`
})

/** 压力位是沿用早先某天填的，不是当天填的——必须标出来，不能冒充成今天的判断。 */
const staleLevels = computed(() => {
  const source = report.value?.levels_source_date
  return source && report.value && source !== report.value.trade_date ? source : null
})

async function csrf() {
  if (!auth.csrfToken) await auth.loadCsrf()
  if (!auth.csrfToken) throw new Error('无法取得写入保护令牌')
  return auth.csrfToken
}

async function saveLevels() {
  if (!report.value) return
  saving.value = true
  try {
    await saveOverviewReportLevels(report.value.trade_date, draft.value, await csrf())
    editingLevels.value = false
    ElMessage.success('已保存')
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function saveSeats() {
  saving.value = true
  try {
    const groups = Object.entries(seatDraft.value).map(([group_key, members]) => ({
      group_key,
      members
    }))
    await saveOverviewReportSeatGroups(groups, await csrf())
    editingSeats.value = false
    ElMessage.success('席位名单已保存')
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

// —— 下半部分的显示 ——

/** 手数。`null` 是「那天不在榜上」，不是零——显示成 0 就等于说他清仓了。 */
function lots(value: string | null) {
  if (value === null) return '—'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toLocaleString('zh-CN')}`
}

/**
 * 筹码（净持仓成本）。白银按运营者报告表的**千元**写法，黄金照常。
 * 白银结算价一万五千多元/千克，写全了一列全是五位数，他那张表历来记 15.7。
 */
function chips(value: string | null, instrument: 'AU' | 'AG') {
  if (value === null) return '—'
  const number = Number(value)
  return instrument === 'AG' ? (number / 1000).toFixed(1) : number.toFixed(2)
}

/** 完整值，挂在 title 上——千元写法丢掉的精度从这里找得回来。 */
function chipsFull(value: string | null) {
  return value === null ? '' : Number(value).toFixed(2)
}

function tone(value: string | null) {
  if (value === null) return ''
  const number = Number(value)
  return number > 0 ? 'up' : number < 0 ? 'down' : ''
}

/** 合计行那一格：今比昨是加仓还是减仓。运营者那张表在这里放的就是「加/减」。 */
function moveLabel(row: ReportSeatRow, side: 'gold' | 'silver') {
  const cell = row[side]
  if (cell.net === null || cell.previous_net === null) return '—'
  const delta = Number(cell.net) - Number(cell.previous_net)
  if (delta === 0) return '平'
  return delta > 0 ? '加' : '减'
}
function moveTone(row: ReportSeatRow, side: 'gold' | 'silver') {
  const label = moveLabel(row, side)
  return label === '加' ? 'up' : label === '减' ? 'down' : ''
}
</script>

<template>
  <el-card shadow="never" class="report">
    <template #header>
      <div class="report-head">
        <h2>{{ title }}</h2>
        <span v-if="report" class="report-date">数据日 {{ report.trade_date }}</span>
      </div>
    </template>

    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else-if="report">
      <!-- ================= 上半：压力位/支撑位（手工填） ================= -->
      <div class="section-head">
        <h3>压力位与支撑位</h3>
        <div class="actions">
          <el-tag v-if="staleLevels" type="warning" size="small" effect="light">
            沿用 {{ staleLevels }} 填的，尚未确认
          </el-tag>
          <template v-if="editingLevels">
            <el-button size="small" :loading="saving" type="primary" @click="saveLevels">
              保存
            </el-button>
            <el-button size="small" @click="editingLevels = false; draft = alignRows(report.levels?.rows)">
              取消
            </el-button>
          </template>
          <el-button v-else size="small" @click="editingLevels = true">编辑</el-button>
        </div>
      </div>

      <div class="table-scroll">
        <table class="grid levels">
          <thead>
            <tr>
              <th></th>
              <th v-for="column in LEVEL_COLUMNS" :key="column">{{ column }}</th>
              <th>偏向</th>
              <th>关注度</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(meta, index) in LEVEL_ROWS"
              :key="meta.key"
              :class="{ 'rating-row': meta.kind === 'rating' }"
            >
              <th class="row-label">{{ meta.label }}</th>
              <td v-for="column in [0, 1, 2]" :key="column">
                <el-input
                  v-if="editingLevels"
                  v-model="draft[index].values[column]"
                  size="small"
                  :placeholder="meta.kind === 'rating' ? '如 5星重关注' : '价位'"
                />
                <span v-else :class="meta.kind === 'rating' ? 'rating-text' : 'level-value'">
                  {{ draft[index].values[column] || '—' }}
                </span>
              </td>

              <td>
                <template v-if="meta.kind === 'rating'"><span class="muted">—</span></template>
                <el-select
                  v-else-if="editingLevels"
                  v-model="draft[index].bias"
                  size="small"
                  style="width: 84px"
                >
                  <el-option label="↑" value="up" />
                  <el-option label="↓" value="down" />
                  <el-option label="无" value="" />
                </el-select>
                <span v-else :class="draft[index].bias === 'up' ? 'up' : draft[index].bias === 'down' ? 'down' : 'muted'">
                  {{ draft[index].bias === 'up' ? '↑' : draft[index].bias === 'down' ? '↓' : '—' }}
                </span>
              </td>

              <td>
                <!-- 星级评分度那行的最后一格也是文字（如「1星高风险」），不是星星。 -->
                <template v-if="meta.kind === 'rating'">
                  <el-input
                    v-if="editingLevels"
                    v-model="draft[index].text"
                    size="small"
                    placeholder="如 1星高风险"
                  />
                  <span v-else class="rating-text">{{ draft[index].text || '—' }}</span>
                </template>
                <el-rate
                  v-else-if="editingLevels"
                  v-model="draft[index].stars as number"
                  :max="5"
                  size="small"
                />
                <span v-else class="stars">
                  {{ '☆'.repeat(draft[index].stars ?? 0) || '—' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ============ 下半：各大席位净持仓与筹码（自动） ============ -->
      <div class="section-head">
        <h3>各大席位净持仓数据与筹码分布</h3>
        <div class="actions">
          <template v-if="editingSeats">
            <el-button size="small" :loading="saving" type="primary" @click="saveSeats">
              保存名单
            </el-button>
            <el-button size="small" @click="editingSeats = false; load()">取消</el-button>
          </template>
          <el-button v-else size="small" @click="editingSeats = true">改席位名单</el-button>
        </div>
      </div>

      <div v-if="editingSeats" class="seat-config">
        <div v-for="group in report.seat_groups" :key="group.group_key" class="seat-group">
          <label>{{ GROUP_LABELS[group.group_key] ?? group.group_key }}</label>
          <el-select
            v-model="seatDraft[group.group_key]"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选席位"
            style="width: 100%"
          >
            <el-option v-for="name in allMembers" :key="name" :label="name" :value="name" />
          </el-select>
        </div>
        <p class="note">
          席位名按<strong>归一后</strong>的写法存（旧名如「乾坤期货」已并入「高盛期货」），
          与席位页同一口径。
        </p>
      </div>

      <div class="table-scroll">
        <table class="grid seats">
          <thead>
            <tr>
              <th>席位名称</th>
              <th>金/昨持仓</th>
              <th>金/今持仓</th>
              <th>筹码</th>
              <th>银/昨持仓</th>
              <th>银/今持仓</th>
              <th>筹码</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in report.rows" :key="row.label" :class="{ total: row.is_total }">
              <th class="row-label">{{ row.label }}</th>
              <td :class="tone(row.gold.previous_net)">{{ lots(row.gold.previous_net) }}</td>
              <td :class="tone(row.gold.net)">{{ lots(row.gold.net) }}</td>
              <td v-if="row.is_total" :class="moveTone(row, 'gold')">{{ moveLabel(row, 'gold') }}</td>
              <td v-else class="chips">{{ chips(row.gold.cost, 'AU') }}</td>
              <td :class="tone(row.silver.previous_net)">{{ lots(row.silver.previous_net) }}</td>
              <td :class="tone(row.silver.net)">{{ lots(row.silver.net) }}</td>
              <td v-if="row.is_total" :class="moveTone(row, 'silver')">{{ moveLabel(row, 'silver') }}</td>
              <td v-else class="chips" :title="chipsFull(row.silver.cost)">
                {{ chips(row.silver.cost, 'AG') }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="note">
        持仓是<strong>合约汇总后的净持仓</strong>（该席位在这个品种各合约上的净仓相加），
        筹码是<strong>净持仓成本（推算）</strong>——净多按多头腿加权、净空按空头腿加权，
        与席位页同一个引擎算出的同一个数。
        <strong>「—」表示那天他不在交易所前 20 榜上，持仓未知，不是零。</strong>
        银的筹码按千元记（15.7 即 15,700 元/千克），鼠标停上去看完整值。
        合计行不给筹码：把几家成本不同的仓位平均成一个数没有意义，那一格放的是今比昨的加减。
      </p>
    </template>
  </el-card>
</template>

<style scoped>
.report-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.report-head h2 {
  margin: 0;
  font-size: 18px;
}
.report-date {
  font-size: 13px;
  color: #909399;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin: 18px 0 10px;
}
.section-head h3 {
  margin: 0;
  font-size: 15px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 宽表在自己的容器里横向滚，页面本身不许横向滚。 */
.table-scroll {
  overflow-x: auto;
}
.grid {
  width: 100%;
  min-width: 640px;
  border-collapse: collapse;
  font-size: 13px;
}
.grid th,
.grid td {
  border: 1px solid #e4e7ed;
  padding: 6px 10px;
  text-align: center;
  white-space: nowrap;
}
.grid thead th {
  background: #fafafa;
  font-weight: 600;
  color: #606266;
}
.grid .row-label {
  background: #fafafa;
  text-align: left;
  font-weight: 600;
}
.grid tr.total .row-label,
.grid tr.total td {
  background: #f5f7fa;
  font-weight: 600;
}
.grid tr.rating-row td {
  background: #fffdf5;
}

.level-value {
  font-weight: 600;
}
.rating-text {
  color: #b45309;
}
.stars {
  color: #e6a23c;
  letter-spacing: 2px;
}
.chips {
  font-weight: 600;
}
.muted {
  color: #c0c4cc;
}
.up {
  color: #c0392b;
  font-weight: 600;
}
.down {
  color: #27ae60;
  font-weight: 600;
}

.seat-config {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  margin-bottom: 12px;
}
.seat-group label {
  display: block;
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}
.note {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: #909399;
}
</style>
