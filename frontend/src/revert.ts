/**
 * 历史回归率的显示口径。
 *
 * 抽出来是为了能测：埋在 `.vue` 的 `<script setup>` 里的函数导不出来，测不到——
 * 席位页的三态吸收律（seatChange.ts）当初就是这么栽的。这两个函数错了都不露馅：
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
