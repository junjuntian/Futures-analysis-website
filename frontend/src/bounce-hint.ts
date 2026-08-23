/**
 * 生猪「卸仓反弹」窗口 → 套利监控页上的一句话(DEC-119)。
 *
 * 引擎(DEC-118)每日算好 `bounce_long`:机构席位组净空、且本轮已卸掉 ≥ long_unload_min。
 * REPORT_LH_SPREAD_SIGNAL_v1 实测:这个状态里生猪跨月**牛市价差**(买近卖远)20 日
 * 均值 +107 元/吨、涨的比例 53%;窗口外无条件 −123 元/吨、37% —— 它挑出的是
 * 牛市价差唯一能赚的窗口。效应一个月内有效,40 日归零。
 *
 * 抽成模块是为了能测:这句话要写清「开/关、为什么、还差多少」,写错不报错。
 * **只是背景,不进套利的任何判据**,与 FG-SA 资金流向同一性质。
 */
export interface BounceState {
  active: boolean
  /** 机构本轮已卸掉的比例,0~1;掉榜日为 null。 */
  unload: number | null
  side: 'net_short' | 'net_long' | null
  /** 阈值,0~1。 */
  min: number
  note: string
}

export interface BounceHint {
  on: boolean
  text: string
}

export function bounceHint(b: BounceState): BounceHint {
  // 与阈值撞整时给一位小数:49.75% 四舍五入成 50% 会写出「只卸掉 50%,未到 50%」。
  const pct = (v: number) =>
    Math.round(v * 100) === Math.round(b.min * 100) && v !== b.min
      ? `${(v * 100).toFixed(1)}%`
      : `${Math.round(v * 100)}%`
  if (b.active) {
    return {
      on: true,
      text: `窗口开:机构净空且本轮已卸掉 ${b.unload === null ? '—' : pct(b.unload)}(≥${pct(b.min)})· 牛市价差可考虑,一个月内`
    }
  }
  if (b.side === 'net_short') {
    return {
      on: false,
      text: `窗口关:机构净空但本轮只卸掉 ${b.unload === null ? '—(掉榜看不清)' : pct(b.unload)},未到 ${pct(b.min)}`
    }
  }
  return { on: false, text: '窗口关:机构未净空 · 牛市价差无条件是逆势' }
}

/** 逐日历史里的一行(引擎 payload 的 bounce_history)。 */
export interface BounceDay {
  d: string
  active: boolean
  unload: number | null
  side: 'net_short' | 'net_long' | null
}

/**
 * 取「所选交易日」那天的窗口状态:历史按日升序,找最后一个 d ≤ date 的行。
 * 所选日早于历史起点时返回 null(那天没有机构数据,不编)。
 * 选了 8/19 就显示 8/19 的机构状态,不是最新的 —— 运营者要拿它手动验证信号。
 */
export function pickBounceDay(history: BounceDay[], date: string): BounceDay | null {
  let hit: BounceDay | null = null
  for (const row of history) {
    if (row.d <= date) hit = row
    else break
  }
  return hit
}

