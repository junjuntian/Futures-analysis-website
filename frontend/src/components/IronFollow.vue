<script setup lang="ts">
/**
 * 铁矿石「跟永安」第二引擎(DEC-178,2026-09-02 运营者拍板)。
 *
 * **它为什么不和玻璃/焦煤共用 HogMoney.vue**:那五个品种的卡片是「阵营 z 分数
 * + 战役」那套,第二引擎只是其中一块;铁矿石没有主引擎,整张卡就是跟随线本身,
 * 外加一份按运营者账户参数缩出来的手数方案。形状不同,单独一个组件。
 *
 * **卡上的丑话是引擎给的,原样印,不做美化。** 第七道闸门(选择偏差)没过 ——
 * 运营者有权在看方向的同时,看见这条线的证据强度到底是多少。
 */
import { computed, onMounted, ref } from 'vue'
import { failureHint } from '../fetch-hint'
import { getSeatNetPosition } from '../api'

interface Run {
  date: string
  exit_date: string | null
  side: 'long' | 'short'
  contract: string
  hold_days: number
  entry_px: number | null
  exit_px: number | null
  ret_pct: number
  open: boolean
}
interface Plan {
  capital: number; use_pct: number; margin_pct: number; mult: number
  price: number; contract: string; side: 'long' | 'short'; lots: number
  budget: number; margin_used: number; notional: number; per_yuan: number
  warn?: string
}
interface Payload {
  instrument: string
  name: string
  data_date: string
  main_contract: string | null
  last_settle: number | null
  member: string
  side: 'long' | 'short' | null
  net: number | null
  run_days: number | null
  run_ret_pct: number | null
  entry_date: string | null
  entry_px: number | null
  flipped_today: boolean
  plan: Plan | null
  history: Run[]
  stats: {
    cum_pct: number; sharpe: number | null; max_dd_pct: number; flips: number
    yearly: Record<string, number>
    segs?: number; win_rate?: number | null; top3_share_pct?: number | null
  }
  note: string
}

const data = ref<Payload | null>(null)
const error = ref('')
/**
 * 永安在铁矿石上的净持仓成本。
 *
 * **不在引擎算**(DEC-143,运营者 2026-08-25:「成本直接引用净持仓的成本,
 * 不需要你单独算」):前端拿浏览器登录态调 `/seats/net-position`,显示的就是
 * 净持仓页**同一台成本引擎**的同一个数。
 */
const cost = ref<string>('')

onMounted(async () => {
  try {
    const res = await fetch(`/smart-money/i_signals.json?t=${Date.now()}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    data.value = await res.json()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
    return
  }
  const d = data.value
  if (!d || !d.side) return
  try {
    const { data: net } = await getSeatNetPosition({
      instrument: 'I', members: [d.member], tradeDate: d.data_date
    })
    const m = net.latest_members.find((x) => x.member === d.member)
    if (!m || m.missing) return
    const raw = d.side === 'long' ? m.long_cost : m.short_cost
    if (raw !== null && raw !== undefined) cost.value = Number(raw).toFixed(1)
  } catch {
    // 取不到就不显示成本,不影响整块
  }
})

const headline = computed(() => {
  const d = data.value
  if (!d) return null
  if (d.side === 'long') return { text: '跟随做多', tone: 'up' as const }
  if (d.side === 'short') return { text: '跟随做空', tone: 'down' as const }
  return { text: '无方向 · 观望', tone: 'muted' as const }
})
/** 历史表**一律倒序**(运营者定的全站规矩):最近的一段摆最上面。 */
const history = computed(() => [...(data.value?.history ?? [])].reverse())
const yearly = computed(() =>
  Object.entries(data.value?.stats.yearly ?? {}).sort(([a], [b]) => a.localeCompare(b)))

function pct(v: number | null | undefined) {
  return v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}
function cls(v: number | null | undefined) {
  return v === null || v === undefined ? '' : v > 0 ? 'up' : v < 0 ? 'down' : ''
}
function money(v: number | null | undefined) {
  return v === null || v === undefined ? '—' : Math.round(v).toLocaleString('en-US')
}
function lots(v: number | null) {
  return v === null ? '—' : Math.abs(v).toLocaleString('en-US')
}
/** payload 里的 **粗体** 与全站一致地渲染。文本来自引擎,不含用户输入。 */
function mdBold(t: string) {
  return t.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
}
</script>

<template>
  <div v-if="error" class="card err">
    读取铁矿石跟随卡失败:{{ error }} —— {{ failureHint(error) }}
  </div>
  <template v-else-if="data">
    <div class="card">
      <h3>
        {{ data.name }} · 跟{{ data.member.replace('期货', '') }}
        <span class="meta">
          数据日 <b>{{ data.data_date }}</b>
          <template v-if="data.main_contract"> · 主力 {{ data.main_contract }}</template>
          · 第二引擎,与主引擎并列
        </span>
      </h3>

      <!-- 结论摆最上面:运营者要的就是「此刻该看多还是看空」。 -->
      <div class="headline" :class="headline?.tone">
        <span class="big">{{ headline?.text }}</span>
        <template v-if="data.side">
          <span class="sep">·</span>
          {{ data.member }}<i v-if="cost" class="cost-at">@{{ cost }}</i>
          <span class="sep">·</span>
          净{{ data.side === 'long' ? '多' : '空' }} {{ lots(data.net) }} 手
          <span class="sep">·</span>
          进场 {{ data.entry_date }}
          <template v-if="data.entry_px"> @{{ data.entry_px }}</template>
          <span class="sep">·</span>
          持有 {{ data.run_days }} 日
          <span class="sep">·</span>
          本轮 <span :class="cls(data.run_ret_pct)">{{ pct(data.run_ret_pct) }}</span>
          <span v-if="data.flipped_today" class="flip">今日翻向 · 次日开盘反手</span>
        </template>
      </div>

      <!-- 手数方案:按运营者给的账户参数缩放。参数原样印在表头,
           免得日后看见一个手数却不知道它是按什么算出来的。 -->
      <template v-if="data.plan">
        <h4>
          跟随方案
          <span class="meta">
            本金 {{ money(data.plan.capital) }} · 动用 {{ data.plan.use_pct }}%
            · 保证金 {{ data.plan.margin_pct }}% · {{ data.plan.mult }} 吨/手
          </span>
        </h4>
        <table class="plan">
          <tr>
            <td class="k">方向</td>
            <td>
              <span class="tag" :class="data.plan.side === 'long' ? 'up' : 'down'">
                {{ data.plan.side === 'long' ? '做多' : '做空' }} {{ data.plan.contract }}
              </span>
            </td>
            <td class="k">手数</td>
            <td class="num"><b>{{ data.plan.lots }}</b> 手</td>
          </tr>
          <tr>
            <td class="k">现价</td>
            <td class="num">{{ data.plan.price }}</td>
            <td class="k">合约价值</td>
            <td class="num">{{ money(data.plan.notional) }} 元</td>
          </tr>
          <tr>
            <td class="k">占用保证金</td>
            <td class="num">{{ money(data.plan.margin_used) }} 元</td>
            <td class="k">预算</td>
            <td class="num cost">{{ money(data.plan.budget) }} 元</td>
          </tr>
          <tr>
            <td class="k">每波动 1 元/吨</td>
            <td class="num">{{ money(data.plan.per_yuan) }} 元</td>
            <td class="k cost">口径</td>
            <td class="cost">按当日结算价与主力合约</td>
          </tr>
        </table>
        <p v-if="data.plan.warn" class="warn-line">{{ data.plan.warn }}</p>
      </template>

      <div class="stats">
        回放 <b>{{ data.stats.segs ?? data.history.length }} 段</b>
        <template v-if="data.stats.win_rate !== null && data.stats.win_rate !== undefined">
          · 胜 {{ data.stats.win_rate }}%
        </template>
        · 累计 <span :class="cls(data.stats.cum_pct)">{{ data.stats.cum_pct }}%</span>
        · 夏普 <b>{{ data.stats.sharpe ?? '—' }}</b>
        · 最大回撤 <span class="down">{{ data.stats.max_dd_pct }}%</span>
        · 翻向 {{ data.stats.flips }} 次
      </div>
      <div class="stats">
        逐年
        <template v-for="[y, v] in yearly" :key="y">
          <span class="sep">·</span>{{ y }} <span :class="cls(v)">{{ v > 0 ? '+' : '' }}{{ v }}%</span>
        </template>
      </div>
      <!-- 集中度必须和收益并排显示:累计 +118.9% 与「三段吃掉大半利润」是两回事。 -->
      <div v-if="data.stats.top3_share_pct" class="stats warn-line">
        <b>最赚的 3 段占了全部段收益的 {{ data.stats.top3_share_pct }}%</b>
        —— 平均值好看不代表每一段都好看。
      </div>

      <table class="hist">
        <tr class="head">
          <td>进场</td><td class="num">进场价</td>
          <td>出场</td><td class="num">出场价</td>
          <td>方向</td><td class="num">持有</td>
          <td class="num">照它做</td><td></td>
        </tr>
        <tr v-for="(h, i) in history" :key="i">
          <td>{{ h.date }}</td>
          <td class="num">
            {{ h.entry_px ?? '—' }}<i v-if="h.contract" class="con">{{ h.contract }}</i>
          </td>
          <td>{{ h.exit_date ?? '—' }}</td>
          <td class="num">
            <template v-if="h.exit_px !== null">{{ h.exit_px }}</template>
            <template v-else-if="data.last_settle">
              <span class="cost">现价 {{ data.last_settle }}</span>
            </template>
            <template v-else>—</template>
          </td>
          <td>
            <span class="tag" :class="h.side === 'long' ? 'up' : 'down'">
              {{ h.side === 'long' ? '做多' : '做空' }}
            </span>
          </td>
          <td class="num">{{ h.hold_days }} 日</td>
          <td class="num" :class="cls(h.ret_pct)">{{ pct(h.ret_pct) }}</td>
          <td class="num cost">{{ h.open ? '进行中' : (h.ret_pct > 0 ? '赢' : '输') }}</td>
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
h4 { margin: 16px 0 8px; font-size: 14px; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.meta { font-weight: 400; font-size: 13px; color: var(--el-text-color-secondary); }
.headline {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px;
  font-size: 14px; padding: 10px 12px; border-radius: 6px;
  background: var(--el-fill-color-light); margin-bottom: 14px;
}
.headline .big { font-size: 18px; font-weight: 700; }
.headline.up .big { color: var(--tv-up); }
.headline.down .big { color: var(--tv-down); }
.headline.muted .big { color: var(--el-text-color-secondary); }
.flip {
  font-size: 12px; margin-left: 6px; padding: 1px 7px; border-radius: 3px;
  color: var(--tv-warn); border: 1px solid var(--tv-warn);
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
.plan td { padding: 5px 8px 5px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.plan .k { width: 104px; color: var(--el-text-color-secondary); }
.hist { margin-top: 12px; }
.hist td { padding: 4px 8px 4px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.hist .head td { color: var(--el-text-color-secondary); font-size: 12px; border-bottom: 1px solid var(--tv-border); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.cost { color: var(--el-text-color-secondary); }
.tag { font-size: 12px; padding: 1px 7px; border-radius: 3px; background: var(--el-fill-color-light); }
.tag.up { color: var(--tv-up); }
.tag.down { color: var(--tv-down); }
.cost-at { font-style: normal; color: var(--el-text-color-secondary); margin-left: 6px; }
.con { font-style: normal; font-size: 11px; color: var(--el-text-color-secondary); margin-left: 4px; }
.up { color: var(--tv-up); }
.down { color: var(--tv-down); }
.stats { margin-top: 10px; font-size: 13px; line-height: 1.8; }
.warn-line { color: var(--tv-warn); }
.sep { color: var(--el-text-color-secondary); margin: 0 2px; }
.note {
  margin: 12px 0 0; font-size: 12px; line-height: 1.8;
  color: var(--el-text-color-secondary);
  border-top: 1px solid var(--el-border-color-lighter); padding-top: 10px;
}
</style>
