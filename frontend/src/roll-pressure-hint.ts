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
 * **DEC-137 已升级为判据**:窗口内散户剩仓处历届高位 → ⚡压力进场·做空价差
 * (PIT 回测 7 触发/12 可判,+2.93%/胜 86%;未触发届 −0.88%);同窗口做多价差
 * 信号按 ⚠ 对待。判据布尔由引擎给(entry_flag/suppress_long),这里只写文案。
 */
export interface RollPressureState {
  active: boolean
  /** 判据(DEC-137,引擎算):窗口内且散户剩仓处历届高位 = ⚡压力进场·做空价差。 */
  entry_flag?: boolean
  suppress_long?: boolean
  /** 镜像判据(DEC-145,只有鸡蛋配):净空剩仓处历届低位 = ⚡做多价差。老 JSON 没有。 */
  entry_flag_long?: boolean
  suppress_short?: boolean
  main: string
  /** 次主力。鸡蛋主力序列不规则,当前届由引擎按量能选,选不出为 null。 */
  next: string | null
  days_left: number
  window: number
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
  const head = `${rp.main} 剩 ${rp.days_left} 日,散户净剩仓 ${fmt(rp.retail_net)} 手${q}`
  if (rp.level === 'high') {
    // ⚡ 只认引擎的 entry_flag(DEC-104:判据在引擎算,前端只渲染)。
    // 展示级品种(焦煤,criterion=False)level 照标 high,但 entry_flag 恒假:
    // 高位只陈述承压,不给进场话术(REPORT_JM_THREE_GAPS_v1:判据无区分度)。
    if (rp.entry_flag) {
      return { on: true, text: `${head} —— ⚡ 压力进场 · 做空价差(空 ${rp.main} 多 ${rp.next ?? '次主力'});窗口内做多价差信号按 ⚠ 对待` }
    }
    return { on: true, text: `${head} —— 处历届高位,近月对次主力承压(展示级,不进判据)` }
  }
  if (rp.level === 'low') {
    // 镜像分支(DEC-145,只有鸡蛋配 mirror):散户**净空**剩仓处历届低位 =
    // 到点被迫买平托近月 → ⚡做多价差。生猪/焦煤 entry_flag_long 恒假,走下面老文案。
    if (rp.entry_flag_long) {
      return { on: true, text: `${head} —— ⚡ 压力进场 · 做多价差(多 ${rp.main} 空 ${rp.next ?? '次主力'});窗口内做空价差信号按 ⚠ 对待` }
    }
    return { on: false, text: `${head} —— 处历届低位,历届低剩仓届价差常反涨,压力不明显` }
  }
  return { on: false, text: `${head} —— 处历届中位,压力一般` }
}
