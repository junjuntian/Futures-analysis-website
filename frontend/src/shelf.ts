/**
 * 平台位阶梯的读时推导（DEC-095 / DEC-098）。
 *
 * 抽出来是为了能测：埋在 `.vue` 的 `<script setup>` 里的函数导不出来，测不到——
 * 席位页的三态吸收律（seatChange.ts）与回归率（revert.ts）当初都是这么栽的。
 * 这里几个函数错了都不露馅：方向反了、盈亏比分子分母倒了，页面照样好好显示。
 */
import type { SpreadMonitorItem, SpreadShelf } from './api'

/**
 * 档位的显示文案。跨度大时给区间，不报一个假精度的均值。
 *
 * 门槛 30 点：并档阈值是 0.5σ，跨度到 30 点意味着这一档已经吃掉了大半个日波动，
 * 再用一个中点数字代表它就是骗人。
 */
export function shelfLabel(shelf: SpreadShelf): string {
  const span = Math.abs(Number(shelf.hi) - Number(shelf.lo))
  return span >= 30 ? `${shelf.lo}~${shelf.hi}` : shelf.level
}

/** 相对现价在上还是在下，写成人话。 */
export function offsetText(shelf: SpreadShelf): string {
  const v = Number(shelf.offset)
  return `${v > 0 ? '上方' : '下方'} ${Math.abs(v)} 点`
}

/**
 * 到达概率的样本下界,与后端 `REACH_MIN_DAYS` 必须一致。
 *
 * 拟合脚本 `research/run_shelf_prob.py` 用 `a[a["T"] >= 5]` 把不足 5 天的观测
 * 排除在样本外,所以后端在这个区间返回 `null`。页面上那一列会全变成「—」,
 * **必须写清为什么**——否则读者会以为是数据没采到,而不是「这里没有依据」。
 */
export const REACH_MIN_DAYS = 5

/** 剩余天数已低于曲线样本下界,概率不可用。 */
export function reachUnavailable(daysLeft: number | null | undefined): boolean {
  return daysLeft !== null && daysLeft !== undefined && daysLeft < REACH_MIN_DAYS
}

export interface DirectionEdge {
  target: SpreadShelf
  stop: SpreadShelf
  /** 到第一目标的点数。 */
  gain: number
  /** 到止损的点数。 */
  risk: number
  /** 盈亏比 = gain ÷ risk。**<1 就是赚的没有亏的多。** */
  ratio: number | null
}

/**
 * 某个方向上离现价最近的目标与止损，以及盈亏比。
 *
 * `down = true` 表示做空价差：目标在下方、止损在上方；做多反过来。
 *
 * 为什么两个方向都要算（DEC-098，运营者提）：**同一个位置，两个方向的风险不对称**。
 * 他的例子是焦煤 2609−2701——二月往下做没问题，到了 −200 附近就只能往上做，
 * 「虽然往下也能赚钱，但风险更大；往上至少能回到 −150 那个平台」。页面此前只显示
 * 拐头那一侧，另一个方向的盈亏比根本看不到，这种不对称只能靠脑子记。
 *
 * `持到期` 判不了这件事：它只说历年最终朝哪边走，不说**去那边的路有多难**。
 * 一个方向持到期为正、却要先扛五百点浮亏去换一百点目标，照样是笔烂交易。
 */
export function edgeOf(item: SpreadMonitorItem, down: boolean): DirectionEdge | null {
  const shelves = item.shelves ?? []
  const nearest = (wantAbove: boolean) =>
    shelves
      .filter((s) => Number(s.offset) !== 0 && Number(s.offset) > 0 === wantAbove)
      .sort((a, b) => Math.abs(Number(a.offset)) - Math.abs(Number(b.offset)))[0] ?? null

  const target = nearest(!down)
  const stop = nearest(down)
  if (!target || !stop) return null

  const gain = Math.abs(Number(target.offset))
  const risk = Math.abs(Number(stop.offset))
  return { target, stop, gain, risk, ratio: risk > 0 ? gain / risk : null }
}
