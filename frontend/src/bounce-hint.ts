/**
 * 生猪「卸仓反弹」窗口 → 套利监控页上的一句话(DEC-119)。
 *
 * 引擎(DEC-118)每日算好 `bounce_long`:机构席位组净空、本轮已卸掉多少。
 * 原版(REPORT_LH_SPREAD_SIGNAL_v1,滚动组 + 50%)把「卸够 50%」当窗口;DEC-127 复验在固定
 * 5 家下已不成立,DEC-128 改成按 2026 回测的「卸仓区间 × 价差位置」写(见 BOUNCE_REF)。
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

/**
 * 「反弹参考区间」= 2026 年历次生猪跨月价差**触底**那天,机构本轮已卸掉的比例落在什么范围
 * (DEC-128,`research/run_lh_unload_at_turn_2026.py`,固定 5 家席位,17 次独立触底日):
 *   最小 0% / 25 分位 10% / 中位 15% / 75 分位 26% / 最大 33%。取四分位 **10%~26%** 当参考区间。
 * 见顶(11 次)时中位 10%、75 分位 30%;2026 机构净空日无条件分布 25 分位 8% / 中位 15% / 75 分位 25%
 * —— **触底时的卸仓比例与平时差不多**,它标不出底部(价差拐头领先机构减仓约 7 日,DEC-121),
 * 这里只是按运营者要求把历次触底时的卸仓范围写给人看。只有 2026 一年;磨底年过去要回头重验。
 * 位置:当年位置 <30% 低位、>70% 高位。
 */
export const BOUNCE_REF = { lo: 0.10, hi: 0.26, lowPos: 0.30, highPos: 0.70, n: 17 } as const

export type PairPosBand = 'low' | 'mid' | 'high'
export function pairPosBand(position: number | null): PairPosBand | null {
  if (position === null || !Number.isFinite(position)) return null
  return position < BOUNCE_REF.lowPos ? 'low' : position > BOUNCE_REF.highPos ? 'high' : 'mid'
}

const POS_LABEL: Record<PairPosBand, string> = { low: '价差低位', mid: '价差中位', high: '价差高位' }

export function bounceHint(b: BounceState, position: number | null = null): BounceHint {
  // 与区间边界撞整时给一位小数,免得写出「已卸掉 26%,已超过 10~26%」。
  const edges = [BOUNCE_REF.lo, BOUNCE_REF.hi]
  const pct = (v: number) =>
    edges.some((e) => Math.round(v * 100) === Math.round(e * 100) && v !== e)
      ? `${(v * 100).toFixed(1)}%`
      : `${Math.round(v * 100)}%`
  if (b.side !== 'net_short') {
    return { on: false, text: '窗口关:机构未净空 · 牛市价差无条件是逆势' }
  }
  const u = b.unload
  const uTxt = u === null ? '—(掉榜看不清)' : pct(u)
  const ref = `反弹参考区间 ${Math.round(BOUNCE_REF.lo * 100)}~${Math.round(BOUNCE_REF.hi * 100)}%`
  const band = pairPosBand(position)
  const rel =
    u === null ? '' : u > BOUNCE_REF.hi ? ',已超过历次触底时的卸仓比例' : u < BOUNCE_REF.lo ? ',还没到历次触底时的卸仓比例' : ',在区间内'
  if (band === null) {
    return { on: false, text: `机构净空且本轮已卸掉 ${uTxt},${ref}${rel} · 组合位置未知` }
  }
  const pos = POS_LABEL[band]
  if (band === 'low') {
    return { on: true, text: `窗口开:机构净空且本轮已卸掉 ${uTxt},${ref}${rel},处于${pos}` }
  }
  return { on: false, text: `窗口关:机构净空且本轮已卸掉 ${uTxt},${ref}${rel},处于${pos}` }
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

