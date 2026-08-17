/**
 * 历史回归率的显示口径。
 *
 * 抽出来是为了能测：埋在 `.vue` 的 `<script setup>` 里的函数导不出来，测不到——
 * 席位页的三态吸收律（seatChange.ts）当初就是这么栽的。这几个函数错了都不露馅：
 * 百分比多一位少一位没人会发现，强弱配色反了更是只会让人默默做出相反的判断。
 */
import type { SpreadRevertStats } from './api'

/** 回归率显示成整数百分比。样本或比率不可用时给破折号，不给 0%。 */
export function revertPct(stats: SpreadRevertStats): string {
  const value = Number(stats.rate)
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : '—'
}

/**
 * 回归率的强弱分档。
 *
 * **不用红绿**：全站红涨绿跌是价格方向的约定（styles.css 顶上的铁律），回归率是个
 * 概率，借那套配色会被读成「红＝看涨」。这里只返回 strong / weak / 中性，配色在
 * 样式里用主色与灰色的深浅表示。
 *
 * 门槛取 55% / 45%：抛硬币是 50%，两边各留 5 个百分点的缓冲，免得样本抖一下就换色。
 */
export function revertTone(stats: SpreadRevertStats): 'strong' | 'weak' | '' {
  const value = Number(stats.rate)
  if (!Number.isFinite(value)) return ''
  if (value >= 0.55) return 'strong'
  if (value <= 0.45) return 'weak'
  return ''
}

/** 点数带符号显示，取整。正数 = 朝回归方向走。 */
export function points(raw: string | null): string | null {
  if (raw === null) return null
  const value = Number(raw)
  if (!Number.isFinite(value)) return null
  const rounded = Math.round(value)
  return rounded > 0 ? `+${rounded}` : `${rounded}`
}

/**
 * 「一直持到窗口止点」的净变化是正是负。
 *
 * 单独拎出来上色，是因为它能揭穿回归率的陷阱：JD2612/JD2701 历年 12 次全都曾跌破
 * 起点（回归率 100%），可一路持到到期的中位是 −166 点，方向反的。只看回归率会把
 * 这种组合读成安全机会，所以负的 drift 必须显眼。
 */
export function driftTone(raw: string | null): 'with' | 'against' | '' {
  if (raw === null) return ''
  const value = Number(raw)
  if (!Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'with' : 'against'
}

/**
 * 资格判定（DEC-063 分层规则第一层）：历年触及率 ≥ 80% 且「持到期」为正。
 *
 * 这不是锦上添花的标签，是盈亏的分水岭：265 个历史报警段留一法回放，合格的
 * 120 段持到底中位 +29% 区间，不合格的 145 段 −26%。门槛写死不做旋钮——
 * 80% 与「为正」都没扫过参数，故意的，扫了就是过拟合。
 * drift 缺失按不合格：资格要的是正证据，「不知道」不放行。
 */
export function isQualified(stats: SpreadRevertStats): boolean {
  const rate = Number(stats.rate)
  const drift = stats.drift_points === null ? NaN : Number(stats.drift_points)
  return Number.isFinite(rate) && Number.isFinite(drift) && rate >= 0.8 && drift > 0
}

/**
 * 拐头反复 = 信号差（DEC-063 修订，运营者拍板）。
 *
 * 近 20 个交易日内穿线 2 次及以上，说明这个组合的拐头不干脆——JM2609/JM2701
 * 八天三次穿线、期间价差打回区间顶，前两次进场按「创报警后新高离场」都得止损。
 * 注意这条门槛是严的：FG2701/SA2701(后续走得很好)也因 08-07 的贴线抖动
 * (0.906→0.855)攒到 ×2 被标——标出来的是「谨慎」，不是「禁止」。
 */
export function isChoppy(turnCrosses: number | null): boolean {
  return turnCrosses !== null && turnCrosses >= 2
}
