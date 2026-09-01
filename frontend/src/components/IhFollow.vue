<script setup lang="ts">
/**
 * 上证50 IH 核心席位看板(DEC-172,运营者两次要求)。
 *
 * **它为什么和别的品种不共用组件**:其余五个品种走的是「阵营 z 分数 + 战役」那套,
 * IH 的信号是「跟某几家席位的在场方向」,形状完全不同 —— 硬塞进 HogMoney.vue
 * 只会两边都别扭。所以单独一个组件、单独一份 `ih_signals.json`。
 *
 * **定位是参考看板,不是过闸的策略。** 引擎那边把丑话写进了 payload 的 note,
 * 这里原样印出来,不做美化 —— 运营者有权在看方向的同时看到证据强度。
 */
import { computed, onMounted, ref } from 'vue'
import { getSeatNetPosition } from '../api'

interface Seat {
  member: string
  on: boolean
  side: 'long' | 'short' | null
  net: number | null
  last_board: string | null
  entry_date: string | null
  days: number | null
  seg_ret_pct: number | null
  rounds: number
}
interface Round {
  entry_date: string
  exit_date: string | null
  side: 'long' | 'short'
  days: number
  /** 执行价:进场 = 信号次日开盘,出场 = 离场信号次日开盘。与段收益同一口径。 */
  entry_px: number | null
  exit_px: number | null
  entry_contract: string | null
  exit_contract: string | null
  /** 段内换过月:两个价属于不同合约,直除**对不上**段收益。界面必须标出来。 */
  rolled: boolean
  mkt_pct: number
  ret_pct: number
}
interface Payload {
  instrument: string
  name: string
  data_date: string
  main_contract: string | null
  last_open: number | null
  carry: number
  state: 'long' | 'short' | 'split' | 'flat'
  on_count: number
  seats: Seat[]
  current: Round | null
  history: Round[]
  stats: {
    rounds: number; wins: number; win_rate: number | null; avg_pct: number | null
    in_days: number; in_pct: number; ann_pct: number | null
    long_same_pct: number | null; edge_pct: number | null; top3_share_pct: number | null
  }
  note: string
}

const data = ref<Payload | null>(null)
const error = ref('')
/**
 * 在场席位的净持仓成本(键=会员名)。
 *
 * **不在引擎算**(DEC-143,运营者 2026-08-25:「成本直接引用净持仓的成本,
 * 不需要你单独算」):前端拿浏览器登录态调 `/seats/net-position`,显示的就是
 * 净持仓页**同一台成本引擎**的同一个数。引擎只出在场状态与净持仓。
 * 取的是**品种汇总档**(不传 contract),与看板上那个净手数同口径。
 */
const costs = ref<Record<string, string>>({})

onMounted(async () => {
  try {
    const res = await fetch(`/smart-money/ih_signals.json?t=${Date.now()}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    data.value = await res.json()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
    return
  }
  const d = data.value
  if (!d) return
  await Promise.all(
    d.seats.filter((s) => s.on).map(async (s) => {
      try {
        // **按它自己的末次上榜日取,不是看板的数据日**(2026-09-01):
        // 掉榜席位按「沿用 20 日」仍算在场,但它在数据日那天不在榜,
        // 接口会返回 missing=true、成本为 null —— 摩根大通末次上榜 08-20,
        // 拿 09-01 去问永远问不到成本。它最后在榜那天的成本才是有意义的那个。
        const { data: net } = await getSeatNetPosition({
          instrument: 'IH', members: [s.member], tradeDate: s.last_board ?? d.data_date
        })
        const m = net.latest_members.find((x) => x.member === s.member)
        if (!m || m.missing) return
        // 净空的取空腿成本、净多取多腿 —— 取错一侧会拿到另一批持仓的均价
        const raw = s.side === 'long' ? m.long_cost : m.short_cost
        if (raw !== null && raw !== undefined) costs.value[s.member] = Number(raw).toFixed(2)
      } catch {
        // 单家取不到就不显示那一个,不影响整块
      }
    })
  )
})

/** 状态一句话。**分歧**是个独立状态,不能和「没人在场」混为一谈。 */
const headline = computed(() => {
  const d = data.value
  if (!d) return null
  if (d.state === 'long') return { text: '跟随做多', tone: 'up' as const }
  if (d.state === 'short') return { text: '跟随做空', tone: 'down' as const }
  if (d.state === 'split') return { text: '方向分歧 · 观望', tone: 'warn' as const }
  return { text: '无人在场 · 观望', tone: 'muted' as const }
})
const onSeats = computed(() => (data.value?.seats ?? []).filter((s) => s.on))
function lots(v: number | null) {
  return v === null ? '—' : Math.abs(v).toLocaleString('en-US')
}
function pct(v: number | null | undefined) {
  return v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}
function cls(v: number | null | undefined) {
  return v === null || v === undefined ? '' : v > 0 ? 'up' : v < 0 ? 'down' : ''
}
/** payload 里的 **粗体** 与全站一致地渲染。文本来自引擎,不含用户输入。 */
function mdBold(t: string) {
  return t.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
}
</script>

<template>
  <div v-if="error" class="card err">读取 IH 看板失败:{{ error }}</div>
  <template v-else-if="data">
    <div class="card">
      <h3>
        {{ data.name }} · 核心席位看板
        <span class="meta">
          数据日 <b>{{ data.data_date }}</b>
          <template v-if="data.main_contract"> · 主力 {{ data.main_contract }}</template>
          · 掉榜沿用 {{ data.carry }} 日
        </span>
      </h3>

      <!-- 结论摆在最上面:运营者要的就是「此刻该看多还是看空」。 -->
      <div class="headline" :class="headline?.tone">
        <span class="big">{{ headline?.text }}</span>
        <template v-if="data.current">
          <span class="sep">·</span>
          <template v-for="(s, i) in onSeats" :key="s.member">
            <span v-if="i" class="sep">、</span>{{ s.member }}<template
              v-if="costs[s.member]"> <i class="cost-at">@{{ costs[s.member] }}</i></template>
          </template>
          <span class="sep">·</span>
          进场 {{ data.current.entry_date }}
          <span class="sep">·</span>
          持有 {{ data.current.days }} 日
          <span class="sep">·</span>
          本轮 <span :class="cls(data.current.ret_pct)">{{ pct(data.current.ret_pct) }}</span>
        </template>
      </div>

      <table class="seats">
        <tr v-for="s in data.seats" :key="s.member">
          <td class="k"><b>{{ s.member }}</b></td>
          <td>
            <!-- **v-if / v-else 之间不许插东西**:2026-09-01 我把成本那个 <i> 插在
                 中间,Vue 的链断掉,摩根大通那一行同时渲染出「在场·净空 697 手」
                 和「不在场」两个标签。成本挪到链外。 -->
            <span v-if="s.on" class="tag" :class="s.side === 'long' ? 'up' : 'down'">
              在场 · {{ s.side === 'long' ? '净多' : '净空' }} {{ lots(s.net) }} 手
            </span>
            <span v-else class="tag muted">不在场</span>
            <!-- 成本口径与净持仓页同一台引擎;字段名是「净持仓成本(推算)」
                 而不是成交均价 —— 我们看不到成交明细。 -->
            <i v-if="s.on && costs[s.member]" class="cost-at">@{{ costs[s.member] }}</i>
          </td>
          <td class="num cost">末次上榜 {{ s.last_board ?? '—' }}</td>
          <td class="num cost">
            <template v-if="s.on">进场 {{ s.entry_date }} · {{ s.days }} 日 ·
              <span :class="cls(s.seg_ret_pct)">{{ pct(s.seg_ret_pct) }}</span>
            </template>
            <template v-else>历史 {{ s.rounds }} 轮</template>
          </td>
        </tr>
      </table>

      <div class="stats">
        回放 <b>{{ data.stats.rounds }} 轮</b> · 胜 {{ data.stats.wins }}
        （{{ data.stats.win_rate }}%） · 均 {{ pct(data.stats.avg_pct) }}/轮
        <span class="sep">·</span>
        在场 {{ data.stats.in_pct }}%（{{ data.stats.in_days }} 日） · 年化
        <span :class="cls(data.stats.ann_pct)">{{ data.stats.ann_pct }}%</span>
        <span class="cost">（同期一律做多 {{ data.stats.long_same_pct }}%，增益
          {{ data.stats.edge_pct }} 个百分点）</span>
      </div>
      <!-- 集中度必须和收益并排显示:平均 +4.7%/轮 与「三轮吃掉大半利润」是两回事。 -->
      <div v-if="data.stats.top3_share_pct !== null" class="stats warn-line">
        <b>最赚的 3 轮占了全部利润的 {{ data.stats.top3_share_pct }}%</b>
        —— 平均值好看不代表每一轮都好看。
      </div>

      <table class="hist">
        <tr class="head">
          <td>进场</td><td class="num">进场价</td>
          <td>出场</td><td class="num">出场价</td>
          <td>方向</td><td class="num">持有</td>
          <td class="num">本段涨跌</td><td class="num">照它做</td><td></td>
        </tr>
        <tr v-for="(h, i) in data.history" :key="i">
          <td>{{ h.entry_date }}</td>
          <!-- 价格后面跟合约代码:段内换过月时两个价属于不同合约,
               不写清楚会让人拿两个价一除、发现对不上段收益又找不到原因。 -->
          <td class="num">
            {{ h.entry_px ?? '—' }}
            <i v-if="h.entry_contract" class="con">{{ h.entry_contract }}</i>
          </td>
          <td>{{ h.exit_date ?? '—' }}</td>
          <td class="num">
            <template v-if="h.exit_px !== null">
              {{ h.exit_px }}<i v-if="h.exit_contract" class="con">{{ h.exit_contract }}</i>
            </template>
            <template v-else-if="data.last_open">
              <span class="cost">现价 {{ data.last_open }}</span>
            </template>
            <template v-else>—</template>
          </td>
          <td><span class="tag" :class="h.side === 'long' ? 'up' : 'down'">{{ h.side === 'long' ? '做多' : '做空' }}</span></td>
          <td class="num">{{ h.days }} 日</td>
          <td class="num cost">{{ pct(h.mkt_pct) }}</td>
          <td class="num" :class="cls(h.ret_pct)">{{ pct(h.ret_pct) }}</td>
          <td class="num cost">
            {{ h.exit_date ? (h.ret_pct > 0 ? '赢' : '输') : '进行中' }}
            <i v-if="h.rolled" class="roll" title="段内换过月:两个价属于不同合约，直除对不上段收益">换月</i>
          </td>
        </tr>
      </table>

      <p class="note" v-html="mdBold(data.note)"></p>
    </div>
  </template>
</template>

<style scoped>
.card {
  background: var(--tv-panel, #fff);
  border: 1px solid var(--tv-border, #e5e7eb);
  border-radius: 8px;
  padding: 16px 18px;
}
.err { color: var(--tv-warn); }
h3 { margin: 0 0 12px; font-size: 16px; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.meta { font-weight: 400; font-size: 13px; color: var(--el-text-color-secondary); }
.headline {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px;
  font-size: 14px; padding: 10px 12px; border-radius: 6px;
  background: var(--el-fill-color-light); margin-bottom: 14px;
}
.headline .big { font-size: 18px; font-weight: 700; }
.headline.up .big { color: var(--tv-up); }
.headline.down .big { color: var(--tv-down); }
.headline.warn .big { color: var(--tv-warn); }
.headline.muted .big { color: var(--el-text-color-secondary); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
.seats td { padding: 5px 8px 5px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.seats .k { width: 96px; }
.hist { margin-top: 12px; }
.hist td { padding: 4px 8px 4px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.hist .head td { color: var(--el-text-color-secondary); font-size: 12px; border-bottom: 1px solid var(--tv-border); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.cost { color: var(--el-text-color-secondary); }
.tag { font-size: 12px; padding: 1px 7px; border-radius: 3px; background: var(--el-fill-color-light); }
.tag.up { color: var(--tv-up); }
.tag.down { color: var(--tv-down); }
.tag.muted { color: var(--el-text-color-secondary); }
.cost-at { font-style: normal; color: var(--el-text-color-secondary); margin-left: 6px; }
.con { font-style: normal; font-size: 11px; color: var(--el-text-color-secondary); margin-left: 4px; }
.roll {
  font-style: normal; font-size: 11px; margin-left: 6px;
  color: var(--tv-warn); border: 1px solid var(--tv-warn);
  border-radius: 3px; padding: 0 3px;
}
.up { color: var(--tv-up); }
.down { color: var(--tv-down); }
.stats { margin-top: 12px; font-size: 13px; line-height: 1.8; }
.warn-line { color: var(--tv-warn); }
.sep { color: var(--el-text-color-secondary); margin: 0 2px; }
.note {
  margin: 12px 0 0; font-size: 12px; line-height: 1.8;
  color: var(--el-text-color-secondary);
  border-top: 1px solid var(--el-border-color-lighter); padding-top: 10px;
}
</style>
