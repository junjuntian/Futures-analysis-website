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
 * 2026 年回测出来的「卸仓 × 价差位置」参考(DEC-128,`research/run_lh_unload_spread_2026.py`,
 * 固定 5 家席位、机构净空日、生猪跨月 16 个组合 893 组合日):
 *   · 价差**低位**(当年位置 <30%)且机构本轮只卸掉 **10%~20%**:之后 20 日价差 +174 元/吨、涨 65%
 *     —— 这是反弹的参考区间;卸 0~10% 时 +69、54%(早了一点);
 *   · 低位但已卸掉 >20%:20 日 −89~−157、涨 27%~38% —— 反弹多半已走完(卸仓比价差拐头晚约 7 日,DEC-121);
 *   · 价差**高位**(>70%):不论卸掉多少 20 日都是 −289~−465、涨 ≤25% —— 牛市价差别追;
 *   · 中间(30%~70%):20 日 −154~−298。
 * **只有 2026 一年、约 5 个独立波段,不是全样本验证**;磨底年过去要回头重验。
 * 运营者 2026-08-23 要求页面按「已卸掉 xx%、反弹参考区间 xx~xx、处于价差高位/低位」写。
 */
export const BOUNCE_REF = { lo: 0.10, hi: 0.20, lowPos: 0.30, highPos: 0.70 } as const

export type PairPosBand = 'low' | 'mid' | 'high'
export function pairPosBand(position: number | null): PairPosBand | null {
  if (position === null || !Number.isFinite(position)) return null
  return position < BOUNCE_REF.lowPos ? 'low' : position > BOUNCE_REF.highPos ? 'high' : 'mid'
}

const POS_LABEL: Record<PairPosBand, string> = { low: '价差低位', mid: '价差中位', high: '价差高位' }

export function bounceHint(b: BounceState, position: number | null = null): BounceHint {
  // 与阈值撞整时给一位小数:49.75% 四舍五入成 50% 会写出「只卸掉 50%,未到 50%」。
  const pct = (v: number) =>
    Math.round(v * 100) === Math.round(b.min * 100) && v !== b.min
      ? `${(v * 100).toFixed(1)}%`
      : `${Math.round(v * 100)}%`
  if (b.side !== 'net_short') {
    return { on: false, text: '窗口关:机构未净空 · 牛市价差无条件是逆势' }
  }
  const u = b.unload
  const uTxt = u === null ? '—(掉榜看不清)' : pct(u)
  const ref = `反弹参考区间 ${Math.round(BOUNCE_REF.lo * 100)}~${Math.round(BOUNCE_REF.hi * 100)}%`
  const band = pairPosBand(position)
  if (band === null) {
    // 没有位置(组合太新)就只报卸仓与区间,不判高低位
    return { on: false, text: `机构净空且本轮已卸掉 ${uTxt},${ref} · 组合位置未知` }
  }
  const pos = POS_LABEL[band]
  if (band === 'high') {
    return { on: false, text: `窗口关:机构净空且本轮已卸掉 ${uTxt},${ref},处于${pos} · 2026 高位不论卸多少 20 日均负,牛市价差别追` }
  }
  if (band === 'mid') {
    return { on: false, text: `窗口关:机构净空且本轮已卸掉 ${uTxt},${ref},处于${pos} · 2026 中位 20 日 −154~−298 元/吨` }
  }
  // 低位
  if (u === null) return { on: false, text: `机构净空但掉榜看不清卸仓,${ref},处于${pos}` }
  if (u >= BOUNCE_REF.lo && u <= BOUNCE_REF.hi) {
    return { on: true, text: `窗口开:机构净空且本轮已卸掉 ${uTxt},在${ref}内,处于${pos} · 牛市价差可考虑(2026:20 日 +174 元/吨、涨 65%)` }
  }
  if (u < BOUNCE_REF.lo) {
    return { on: false, text: `窗口半开:机构净空且本轮只卸掉 ${uTxt},未到${ref},处于${pos} · 早了一点(2026:20 日 +69、涨 54%)` }
  }
  return { on: false, text: `窗口关:机构净空且本轮已卸掉 ${uTxt},已过${ref},处于${pos} · 反弹多半已走完(2026:20 日 −89~−157)` }
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

