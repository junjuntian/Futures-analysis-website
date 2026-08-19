<script setup lang="ts">
/**
 * 生猪机构资金:读引擎产出的 hog_signals.json,只渲染不计算。
 *
 * 与金银**分成两个组件**不是重复:两套信号形态根本不同——金银是逐家席位权重 ×
 * 多席位共振,生猪是八家合计流向(研究阶段实测:生猪逐家事件整体胜率只有 50%,
 * 而合计流向控制动量后 t=5.4~7.5)。硬塞进同一套模板只会让两边都别扭。
 *
 * 界面上有一件事是硬要求:**做多信号必须标注「未经验证」**。回测里多头 15 笔
 * 累计仅 +4.5%,而样本期内机构合计净持仓一天都没转成净多——它符合运营者
 * 「等机构转多就转向」的策略意图,但没有数据背书,不能让它看起来和空头一样可信。
 */
import { computed, onMounted, ref } from 'vue'

interface MemberLeg {
  member: string
  net: number
  change: number | null
  on_board: boolean
}
interface HogTrade {
  side: 'short' | 'long'
  entry_date: string
  exit_date: string | null
  entry_px: number | null
  exit_px: number | null
  contract: string
  ret_pct: number
  hold_days: number
  exit_reason: string | null
}
interface HogPayload {
  name: string
  unit: string
  data_date: string
  computed_at: string
  state: string
  contract: string
  price: number | null
  signal: {
    z: number | null
    enter: number
    net: number | null
    change: number | null
    win: number
    suggested_position: number | null
  }
  position: HogTrade | null
  members: MemberLeg[]
  group_log: Array<{ date: string; members: string[]; alpha: Record<string, number> }>
  history: HogTrade[]
  stats: {
    trades: number
    win_rate: number | null
    avg_pct: number | null
    cum_pct: number | null
    short_trades: number
    long_trades: number
  }
  rules: Record<string, unknown>
  caveats: string[]
}

const data = ref<HogPayload | null>(null)
const error = ref('')
const tab = ref<'today' | 'history' | 'group' | 'rules'>('today')
const page = ref(1)
const PAGE_SIZE = 20

onMounted(async () => {
  try {
    // 与金银同一条路:引擎写静态 JSON,nginx 直接服务。带时间戳绕开缓存。
    const res = await fetch(`/smart-money/hog_signals.json?t=${Date.now()}`)
    if (!res.ok) throw new Error(`读取失败 ${res.status}`)
    data.value = await res.json()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取生猪信号失败'
  }
})

const fmt = (v: number | null | undefined, d = 0) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toLocaleString('zh-CN', {
    minimumFractionDigits: d, maximumFractionDigits: d
  })
const pct = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
/** 期货看盘惯例:涨红跌绿。空单赚钱时价格在跌,但盈亏本身仍按正负着色。 */
const pnlClass = (v: number | null | undefined) =>
  v === null || v === undefined ? '' : v > 0 ? 'red' : v < 0 ? 'green' : ''

const sideText = (s: string) => (s === 'short' ? '做空' : '做多')

/**
 * 持仓是否跨越过主力换月。
 *
 * 生猪主力换得勤,而各合约价差最大到 49%——一旦跨了换月,**进场价与现价就不在
 * 同一个合约上**,并排摆着会让人拿它们相减。实盘当前这笔就是:2026-08-04 在
 * LH2609 进场 @10825,现在主力已是 LH2611 报 12485,看着像涨 15%,而真实收益
 * 是 +5.23%(逐日收益累积,换月日用新合约自己的前一日结算价)。必须标出来。
 */
const rolled = computed(() =>
  !!data.value?.position && data.value.position.contract !== data.value.contract)

/** 信号强度条:z 相对进场门槛的比例,超过门槛就满格。 */
const zRatio = computed(() => {
  const z = data.value?.signal.z
  const e = data.value?.signal.enter ?? 1
  if (z === null || z === undefined) return 0
  return Math.min(Math.abs(z) / e, 1)
})

const rows = computed(() => {
  const h = data.value?.history ?? []
  // 新的在前:历史表看的是最近发生了什么
  const sorted = [...h].reverse()
  const start = (page.value - 1) * PAGE_SIZE
  return sorted.slice(start, start + PAGE_SIZE)
})
const totalPages = computed(() =>
  Math.max(1, Math.ceil((data.value?.history.length ?? 0) / PAGE_SIZE)))

/** 多空各自的统计。做多那条支路要单独摆出来——它是未验证的那条。 */
const bySide = computed(() => {
  const h = data.value?.history ?? []
  const calc = (side: string) => {
    const s = h.filter((t) => t.side === side)
    if (!s.length) return null
    const cum = s.reduce((a, t) => a * (1 + t.ret_pct / 100), 1) - 1
    return {
      n: s.length,
      cum: cum * 100,
      win: (s.filter((t) => t.ret_pct > 0).length / s.length) * 100,
      avg: s.reduce((a, t) => a + t.ret_pct, 0) / s.length
    }
  }
  return { short: calc('short'), long: calc('long') }
})
</script>

<template>
  <div v-if="error" class="err">{{ error }}</div>
  <div v-else-if="!data" class="loading">正在读取生猪信号数据…</div>

  <template v-else>
    <p class="sub">
      跟随机构合计资金流向,每日收盘后自动计算。数据日期 <b>{{ data.data_date }}</b> ·
      计算于 {{ data.computed_at }}
    </p>

    <!-- 持仓状态条。空仓不渲染,与金银页同一条规矩。 -->
    <div v-if="data.position" class="symbol-strip">
      <div class="strip-row">
        <!-- 合约用**当前主力**:旁边那个价格就是它的,写成进场时的合约会对不上 -->
        <span class="strip-name">{{ data.name }} {{ data.contract }}</span>
        <span class="strip-price" :class="pnlClass(data.position.ret_pct)">
          {{ fmt(data.price, 0) }}
        </span>
        <span class="strip-pct" :class="pnlClass(data.position.ret_pct)">
          {{ pct(data.position.ret_pct) }}
        </span>
        <span class="strip-pill" :class="{ unverified: data.position.side === 'long' }">
          {{ sideText(data.position.side) }}中 {{ data.position.hold_days }} 日
          <template v-if="data.position.side === 'long'"> · 未验证</template>
        </span>
        <span class="strip-metrics">
          <span class="m">
            <i>进场{{ rolled ? `(${data.position.contract})` : '' }}</i>
            <b>{{ fmt(data.position.entry_px, 0) }}</b>
          </span>
          <span class="m"><i>信号强度</i><b>{{ fmt(data.signal.z, 2) }}</b></span>
          <span class="m"><i>机构合计净持仓</i><b>{{ fmt(data.signal.net) }}</b></span>
        </span>
      </div>
    </div>

    <div class="tabbar">
      <div class="tab" :class="{ on: tab === 'today' }" @click="tab = 'today'">今日信号</div>
      <div class="tab" :class="{ on: tab === 'history' }" @click="tab = 'history'">历史信号</div>
      <div class="tab" :class="{ on: tab === 'group' }" @click="tab = 'group'">席位组</div>
      <div class="tab" :class="{ on: tab === 'rules' }" @click="tab = 'rules'">策略方案</div>
    </div>

    <!-- ------------------------------------------------ 今日信号 -->
    <template v-if="tab === 'today'">
      <div class="cards">
        <div class="card">
          <h3>
            <span class="dot" :class="data.position ? (data.position.side === 'short' ? 'green' : 'red') : 'gray'" />
            {{ data.name }} — {{ data.state }}
          </h3>
          <div v-if="data.position" class="big" :class="pnlClass(data.position.ret_pct)">
            {{ pct(data.position.ret_pct) }}
          </div>
          <div v-else class="big gray">无持仓</div>
          <div class="kv"><span class="k">现价</span><span class="v">{{ fmt(data.price) }} · {{ data.contract }}</span></div>
          <template v-if="data.position">
            <div class="kv"><span class="k">进场</span>
              <span class="v">{{ data.position.entry_date }} @ {{ fmt(data.position.entry_px) }}
                <template v-if="rolled">· {{ data.position.contract }}</template>
              </span></div>
            <div class="kv"><span class="k">方向</span><span class="v">{{ sideText(data.position.side) }}</span></div>
            <div class="kv"><span class="k">持有</span><span class="v">{{ data.position.hold_days }} 个交易日</span></div>
          </template>
          <p v-if="rolled" class="note">
            这笔持仓跨过了主力换月:进场在 {{ data.position!.contract }},现价是
            {{ data.contract }} 的。**两个价格不在同一个合约上,不要相减**——
            收益按逐日累积算(换月日用新合约自己的前一日结算价),生猪各合约价差
            最大到 49%,相减会得出一个完全错误的数。
          </p>
          <div v-else-if="!data.position" class="kv">
            <span class="k">进场条件</span>
            <span class="v">信号强度需达 {{ data.signal.enter }}(现 {{ fmt(data.signal.z, 2) }})</span>
          </div>
        </div>

        <div class="card">
          <h3>机构合计流向</h3>
          <div class="meter">
            <div class="meter-bar">
              <div class="meter-fill" :class="(data.signal.z ?? 0) < 0 ? 'green' : 'red'"
                   :style="{ width: `${zRatio * 100}%` }" />
            </div>
            <div class="meter-label">
              <span>{{ (data.signal.z ?? 0) < 0 ? '机构在加空' : '机构在减空/加多' }}</span>
              <b>{{ fmt(data.signal.z, 2) }}</b>
            </div>
          </div>
          <div class="kv"><span class="k">合计净持仓</span><span class="v">{{ fmt(data.signal.net) }} 手</span></div>
          <div class="kv"><span class="k">{{ data.signal.win }} 日变化</span>
            <span class="v" :class="pnlClass(data.signal.change)">{{ fmt(data.signal.change) }} 手</span></div>
          <div class="kv"><span class="k">建议仓位强度</span><span class="v">{{ fmt(data.signal.suggested_position, 2) }}</span></div>
          <p class="note">
            信号读的是**这几家合起来往哪个方向调仓**,不是谁的仓位大。
            负值=加空、正值=减空或加多。
          </p>
        </div>

        <div class="card">
          <h3>组内各家(共 {{ data.members.length }} 家)</h3>
          <div v-for="m in data.members" :key="m.member" class="kv">
            <span class="k">{{ m.member }}</span>
            <span class="v">
              <template v-if="m.on_board">
                {{ fmt(m.net) }} 手
                <span v-if="m.change !== null" :class="pnlClass(m.change)">
                  ({{ m.change >= 0 ? '+' : '' }}{{ fmt(m.change) }})
                </span>
              </template>
              <span v-else class="gray">当日未上榜</span>
            </span>
          </div>
          <p class="note">席位组每 3 个月按历史择时收益重选一次,不是固定名单。</p>
        </div>
      </div>

      <!-- 未验证提示必须在首屏,不能藏进策略方案页 -->
      <div v-if="data.position?.side === 'long'" class="caveat-box">
        <b>当前是做多信号,而做多这条支路没有数据背书。</b>
        回测里多头 15 笔累计仅 +4.5%,且样本期内机构合计净持仓一天都没转成净多。
        它符合「等机构转多就转向」的意图,但请按未验证规则对待。
      </div>
    </template>

    <!-- ------------------------------------------------ 历史信号 -->
    <template v-else-if="tab === 'history'">
      <div class="cards">
        <div class="card">
          <h3>全部 {{ data.stats.trades }} 笔</h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(data.stats.cum_pct)">{{ pct(data.stats.cum_pct) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ data.stats.win_rate }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(data.stats.avg_pct) }}</span></div>
        </div>
        <div v-if="bySide.short" class="card">
          <h3>做空 {{ bySide.short.n }} 笔<span class="badge ok">有回测支撑</span></h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(bySide.short.cum)">{{ pct(bySide.short.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ bySide.short.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(bySide.short.avg) }}</span></div>
        </div>
        <div v-if="bySide.long" class="card">
          <h3>做多 {{ bySide.long.n }} 笔<span class="badge warn">未验证</span></h3>
          <div class="kv"><span class="k">累计</span><span class="v" :class="pnlClass(bySide.long.cum)">{{ pct(bySide.long.cum) }}</span></div>
          <div class="kv"><span class="k">胜率</span><span class="v">{{ bySide.long.win.toFixed(1) }}%</span></div>
          <div class="kv"><span class="k">单笔均值</span><span class="v">{{ pct(bySide.long.avg) }}</span></div>
        </div>
      </div>

      <table class="tbl">
        <thead>
          <tr><th>方向</th><th>进场</th><th>出场</th><th>合约</th><th class="num">进场价</th>
            <th class="num">出场价</th><th class="num">收益</th><th class="num">持有</th><th>出场原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in rows" :key="i">
            <td><span class="side" :class="t.side">{{ sideText(t.side) }}</span></td>
            <td>{{ t.entry_date }}</td>
            <td>{{ t.exit_date ?? '持有中' }}</td>
            <td>{{ t.contract }}</td>
            <td class="num">{{ fmt(t.entry_px) }}</td>
            <td class="num">{{ fmt(t.exit_px) }}</td>
            <td class="num" :class="pnlClass(t.ret_pct)">{{ pct(t.ret_pct) }}</td>
            <td class="num">{{ t.hold_days }} 日</td>
            <td>{{ t.exit_reason ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="totalPages > 1" class="pager">
        <button :disabled="page <= 1" @click="page--">上一页</button>
        <span>{{ page }} / {{ totalPages }}</span>
        <button :disabled="page >= totalPages" @click="page++">下一页</button>
      </div>
      <p class="note">
        收益是**毛收益**,未扣手续费与滑点(回测按单边 0.05% 估算,36 笔合计约 3.6 个百分点)。
      </p>
    </template>

    <!-- ------------------------------------------------ 席位组 -->
    <template v-else-if="tab === 'group'">
      <div class="cards">
        <div class="card wide">
          <h3>重选历史(每 3 个月一次)</h3>
          <p class="note">
            括号里是该家截至重选时点的**择时收益**(亿元)——把「一直挂着同样大小的仓
            不动」能赚到的钱扣掉之后剩下的部分。按它选人,而不是按谁赚得多:
            实测按总盈亏选样本外 t=3.57、按仓位规模选 t=0.11,按择时收益选 t=5.22。
          </p>
          <div v-for="g in [...data.group_log].reverse()" :key="g.date" class="glog">
            <b>{{ g.date }}</b>
            <span v-for="m in g.members" :key="m" class="chip">
              {{ m }}<i>{{ g.alpha[m]?.toFixed(2) ?? '—' }}</i>
            </span>
          </div>
        </div>
      </div>
    </template>

    <!-- ------------------------------------------------ 策略方案 -->
    <template v-else>
      <div class="cards">
        <div class="card wide">
          <h3>怎么算的</h3>
          <ol class="rules">
            <li><b>选人</b>:每 3 个月,按截至当时的**择时收益**排序取前 5 家。只用当时
              之前的数据,不看未来。</li>
            <li><b>信号</b>:这 5 家在**全品种合约上的合计净持仓**,取 {{ data.signal.win }} 日变化,
              再用滚动标准差无量纲化得到强度 z。<br>
              <span class="hint">为什么用品种合计而不是逐合约:实测 84.6% 的交易日里同日不同合约
              的持仓变化方向相反——那是移仓换月,逐合约会把一次调仓读成两个相反的信号。</span></li>
            <li><b>进场</b>:|z| ≥ {{ data.signal.enter }} 时跟随方向。做多额外要求过去 20 日是跌的。</li>
            <li><b>出场</b>:反向信号 / 硬止损 6% / 持满 40 个交易日。<br>
              <span class="hint">实测三年 36 笔全部由「反向」「持满」「止损」触发,消退条件一次没用上。</span></li>
            <li><b>计价</b>:主力合约,**换月日用新合约自己的前一日结算价**。生猪各合约相对
              主力偏离最大 49%,跨合约相除得到的是价差不是收益。</li>
          </ol>
        </div>
        <div class="card wide">
          <h3>必须知道的边界</h3>
          <ul class="caveats">
            <li v-for="(c, i) in data.caveats" :key="i" v-text="c" />
          </ul>
        </div>
      </div>
    </template>
  </template>
</template>

<style scoped>
.sub { color: var(--tv-text-secondary); margin: 0 0 22px; }
.loading { padding: 60px; text-align: center; color: var(--tv-text-secondary); }
.err { padding: 20px; background: var(--tv-up-bg); border: 1px solid var(--tv-up);
  border-radius: 6px; color: var(--tv-up); }
.red { color: var(--tv-up); } .green { color: var(--tv-down); } .gray { color: var(--tv-text-muted); }

.symbol-strip {
  position: sticky; top: 0; z-index: 10;
  background: var(--tv-bg-card); border: 1px solid var(--tv-border);
  border-radius: var(--tv-radius); padding: 10px 16px; margin-bottom: 14px;
  box-shadow: var(--tv-shadow);
}
.strip-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.strip-name { font-size: 15px; font-weight: 600; color: var(--tv-text); white-space: nowrap; }
.strip-price { font-size: 24px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums; }
.strip-pct { font-size: 14px; font-variant-numeric: tabular-nums; }
.strip-pill {
  background: var(--tv-warn-bg); color: var(--tv-warn); font-size: 12px;
  padding: 3px 10px; border-radius: var(--tv-radius-sm); white-space: nowrap;
}
/* 未验证的做多持仓要看得出不一样,不能和空头持仓长同一个样 */
.strip-pill.unverified { background: var(--tv-up-bg); color: var(--tv-up); }
.strip-metrics { display: flex; gap: 16px; margin-left: auto; flex-wrap: wrap; }
.strip-metrics .m { display: flex; flex-direction: column; gap: 2px; }
.strip-metrics i { font-style: normal; font-size: 11px; color: var(--tv-text-muted); }
.strip-metrics b { font-size: 13px; font-variant-numeric: tabular-nums; }

.tabbar { display: flex; margin-bottom: 16px; border-bottom: 2px solid var(--tv-border); }
.tab { padding: 9px 22px; cursor: pointer; color: var(--tv-text-secondary);
  border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab.on { color: var(--tv-blue); border-color: var(--tv-blue); font-weight: 600; }

.cards { display: flex; gap: 16px; margin-bottom: 22px; flex-wrap: wrap; }
.card { background: var(--tv-bg-card); border: 1px solid var(--tv-border);
  border-radius: 6px; padding: 18px 20px; flex: 1; min-width: 290px; }
.card.wide { flex-basis: 100%; }
.card h3 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--tv-text-muted); }
.dot.red { background: var(--tv-up); } .dot.green { background: var(--tv-down); }
.big { font-size: 30px; font-weight: 700; margin: 6px 0 12px; font-variant-numeric: tabular-nums; }
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; gap: 10px; }
.kv .k { color: var(--tv-text-secondary); white-space: nowrap; }
.kv .v { text-align: right; font-variant-numeric: tabular-nums; }
.note { font-size: 12px; color: var(--tv-text-muted); margin: 10px 0 0; line-height: 1.6; }
.hint { font-size: 12px; color: var(--tv-text-muted); }

.meter { margin-bottom: 10px; }
.meter-bar { height: 8px; background: var(--tv-border); border-radius: 4px; overflow: hidden; }
.meter-fill { height: 100%; border-radius: 4px; }
.meter-fill.red { background: var(--tv-up); } .meter-fill.green { background: var(--tv-down); }
.meter-label { display: flex; justify-content: space-between; font-size: 12px;
  color: var(--tv-text-secondary); margin-top: 6px; }

.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500; margin-left: auto; }
.badge.ok { background: var(--tv-down-bg, rgba(8,153,129,.1)); color: var(--tv-down); }
.badge.warn { background: var(--tv-warn-bg); color: var(--tv-warn); }

.caveat-box {
  background: var(--tv-warn-bg); border: 1px solid var(--tv-warn);
  border-radius: 6px; padding: 14px 18px; font-size: 13px; line-height: 1.7;
  color: var(--tv-text); margin-bottom: 22px;
}
.caveat-box b { color: var(--tv-warn); }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 8px 10px; border-bottom: 1px solid var(--tv-border); text-align: left; }
.tbl th { color: var(--tv-text-secondary); font-weight: 500; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.side { font-size: 12px; padding: 2px 8px; border-radius: 4px; }
.side.short { background: var(--tv-down-bg, rgba(8,153,129,.1)); color: var(--tv-down); }
.side.long { background: var(--tv-up-bg); color: var(--tv-up); }

.pager { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 14px; }
.pager button { padding: 4px 12px; border: 1px solid var(--tv-border);
  background: var(--tv-bg-card); border-radius: 4px; cursor: pointer; color: var(--tv-text); }
.pager button:disabled { opacity: .45; cursor: default; }

.glog { padding: 8px 0; border-bottom: 1px solid var(--tv-border); font-size: 13px; }
.glog b { margin-right: 12px; }
.chip { display: inline-flex; align-items: baseline; gap: 4px; margin-right: 8px;
  background: var(--tv-bg); border: 1px solid var(--tv-border);
  border-radius: 4px; padding: 2px 8px; font-size: 12px; }
.chip i { font-style: normal; color: var(--tv-text-muted); font-variant-numeric: tabular-nums; }

.rules { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; }
.rules li { margin-bottom: 6px; }
.caveats { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.9; color: var(--tv-text-secondary); }
</style>
