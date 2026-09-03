/**
 * 生猪「移仓强制流压力表」→ 页面上的一句话(DEC-136)。
 *
 * 引擎每日算好 `roll_pressure`:近月散户多头剩仓、历届锚点分布四分位、剩余日。
 * 机制(REPORT_ROLL_PRESSURE_v1):散户多头集中在近月、窗口止点必须离场、
 * 小资金无承接 → 近月相对次主力被压;散户剩仓越大压得越狠。
 * **机构版被否**(机构能慢慢移仓,没有预测力)——别把这块改成看机构。
 *
 * 统计数字(秩相关/分组收益)只住引擎的 `note` 里,这里不抄第二份 ——
 * 同一个事实两处维护是这个仓库最高产的 bug 源。
 * **DEC-137 升级为判据,DEC-189 与回测口径对齐**:盯的是**被迫方近月**(剩
 * `anchor` 日内那个正被赶出场的合约,此时主力多半已换走),读它**锚点日**那一格的
 * 散户剩仓与历届同一时点的分布比 —— 处高位 → ⚡压力进场·做空价差,每届一次。
 * 判据布尔由引擎给(entry_flag/suppress_long),这里只写文案。
 *
 * **`window` 不是判据窗口**,只管这张表从第几天开始显示;别再拿它写进 ⚡ 的话术。
 */
export interface RollPressureState {
  active: boolean
  /** 判据(DEC-137,引擎算):窗口内且散户剩仓处历届高位 = ⚡压力进场·做空价差。 */
  entry_flag?: boolean
  suppress_long?: boolean
  /** 镜像判据(DEC-145,只有鸡蛋配):净空剩仓处历届低位 = ⚡做多价差。老 JSON 没有。 */
  entry_flag_long?: boolean
  suppress_short?: boolean
  /**
   * 本品种是不是判据级(生猪/鸡蛋 true,焦煤/铁矿石 false)。老 JSON 没有这个键 ——
   * 缺失时**不许**替它猜成展示级:那会把生猪说成「不进判据」(2026-09-03 抓到)。
   */
  criterion?: boolean
  /**
   * 今天是不是有「被迫方近月」(DEC-189)。为真时 `main`/`days_left`/`retail_net`
   * 说的都是**它**,而不是主力;为假时退回主力,只作背景展示,⚡ 不可能亮。
   * 老 JSON 没有这个键。
   */
  forced?: boolean
  /** 被迫方的锚点日(判据读的就是这一天那一格)。没有被迫方时为 null。 */
  anchor_date?: string | null
  /** 被迫方**今天**的净剩仓,只作参考 —— 判据用的是 `retail_net`(锚点日那格)。 */
  retail_net_now?: number | null
  /** 表述对象:被迫方近月,或退回主力。 */
  main: string
  /** 对手腿。鸡蛋主力序列不规则,当前届由引擎按量能选,选不出为 null。 */
  next: string | null
  days_left: number
  /** 展示窗口(剩 ≤window 起显示)。**不是判据窗口**,判据看的是 anchor。 */
  window: number
  /** 判据用的那个数:有被迫方时 = 锚点日那一格;否则 = 今天的主力剩仓。 */
  retail_net: number | null
  hist_q1: number | null
  hist_med: number | null
  hist_q3: number | null
  level: 'high' | 'mid' | 'low' | null
  vol_ratio: number | null
  spread_now: number | null
  anchor: number
  history: Array<{ main: string; date: string; retail_net: number; spread_move_pct: number | null }>
  note: string
}

export interface RollPressureHint {
  /** 高剩仓且在窗口内才亮。 */
  on: boolean
  text: string
}

const fmt = (v: number | null) => (v === null ? '—' : v.toLocaleString('zh-CN'))

export function rollPressureHint(rp: RollPressureState): RollPressureHint {
  if (!rp.active) {
    return {
      on: false,
      text: `未到窗口:${rp.main} 剩 ${rp.days_left} 个交易日(≤${rp.window} 日起看)`
    }
  }
  const q = rp.hist_q1 !== null && rp.hist_q3 !== null
    ? `,历届锚点四分位 ${fmt(rp.hist_q1)}~${fmt(rp.hist_q3)} 手`
    : ''
  // 有被迫方时,判据读的是**锚点日**那一格,不是今天 —— 必须写明是哪天的数,
  // 否则页面上摆着一个和「今」对不上的数字,读的人只会以为哪里算错了(DEC-189)。
  const judged = rp.forced && rp.anchor_date
    ? `锚点日(${rp.anchor_date})散户净剩仓 ${fmt(rp.retail_net)} 手` +
      (rp.retail_net_now !== undefined && rp.retail_net_now !== null
        ? `,今 ${fmt(rp.retail_net_now)} 手` : '')
    : `散户净剩仓 ${fmt(rp.retail_net)} 手`
  const who = rp.forced ? `被迫方 ${rp.main}` : rp.main
  const head = `${who} 剩 ${rp.days_left} 日,${judged}${q}`
  if (rp.level === 'high') {
    // ⚡ 只认引擎的 entry_flag(DEC-104:判据在引擎算,前端只渲染)。
    // 展示级品种(焦煤,criterion=False)level 照标 high,但 entry_flag 恒假:
    // 高位只陈述承压,不给进场话术(REPORT_JM_THREE_GAPS_v1:判据无区分度)。
    if (rp.entry_flag) {
      return { on: true, text: `${head} —— ⚡ 压力进场 · 做空价差(空 ${rp.main} 多 ${rp.next ?? '次主力'});本届做多价差信号按 ⚠ 对待` }
    }
    return { on: true, text: `${head} —— 处历届高位,近月对次主力承压${whyNoBolt(rp)}` }
  }
  if (rp.level === 'low') {
    // 镜像分支(DEC-145,只有鸡蛋配 mirror):散户**净空**剩仓处历届低位 =
    // 到点被迫买平托近月 → ⚡做多价差。生猪/焦煤 entry_flag_long 恒假,走下面老文案。
    if (rp.entry_flag_long) {
      return { on: true, text: `${head} —— ⚡ 压力进场 · 做多价差(多 ${rp.main} 空 ${rp.next ?? '次主力'});本届做空价差信号按 ⚠ 对待` }
    }
    return { on: false, text: `${head} —— 处历届低位,历届低剩仓届价差常反涨,压力不明显` }
  }
  return { on: false, text: `${head} —— 处历届中位,压力一般` }
}

/**
 * ⚡ 没亮的原因,只在「处历届高位」时补一句。三种情况必须分开说:
 *
 * - 展示级品种(焦煤/铁矿石,`criterion:false`):规则如此,永远不亮;
 * - 判据级但**今天没有被迫方**(生猪剩 30~21 日这一段,表已在显示但判据未起算):
 *   会亮,只是还没到 —— DEC-189 之前这一段是**会亮的**,对齐回测后归零;
 * - 老 JSON 没有这几个键:什么都不补,不许替它猜。
 *
 * 原来这一句对所有品种写死「展示级,不进判据」。对焦煤/铁矿石成立,对生猪/鸡蛋
 * 是假话:它们是判据级,只是当下没满足别的条件 —— DEC-168 那次「模板里写死席位名」
 * 同款,而且**单元测试照不到**:测的是有没有渲染,不是渲染成了谁。
 *
 * 老 JSON 没有这几个键(前端先于引擎上线的空窗期,DEC-089):什么都不补,
 * 不许替它猜 —— 猜错方向就是上面那句假话。
 */
function whyNoBolt(rp: RollPressureState): string {
  if (rp.criterion === false) return '(展示级,不进判据)'
  if (rp.criterion !== true || rp.forced === undefined) return ''
  return rp.forced ? '' : `(判据只看被迫方近月剩 ≤${rp.anchor} 日那一段,还没到)`
}
