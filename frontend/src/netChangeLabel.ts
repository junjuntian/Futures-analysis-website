/**
 * 席位净持仓变化的方向文案(「净多+X」/「净空+X」)。
 *
 * 提成独立模块只为一件事:**能被测试直接调到**。埋在 `<script setup>` 里的函数
 * 导不出来(同 shelf.ts / seatChange.ts)。
 *
 * ## 跨零那一支:试过,运营者否了
 *
 * 2026-09-02 一度改成:席位在窗口内跨过 0 时单独说「由净空 A 转净多 B」,理由是
 * 「净多+100,239」这句话在东证身上不成立(它是从净空 60,573 翻到净多 39,666,
 * 多头从没增加过那么多)。
 *
 * **运营者当场否掉:「东证还是按原来显示,那样显示没有错」。** 同一轮里括号的口径
 * 也从「较 5 日前」改成「较昨日」—— 一天之内跨零本来就少见,而「净多+X」读的是
 * **净持仓这个量往哪个方向走了多少**,不是「多头那条腿增加了多少」。按这个读法,
 * 原来的写法没问题。
 *
 * 所以这里只有一条规则:**按今天在哪一边,说净持仓变化的方向与大小**。
 * 别再"顺手"把跨零那支加回来 —— 加过一次,被否了。
 */
export interface NetChange {
  /** 今日净持仓。正=净多,负=净空。 */
  net: number
  /** 较昨日的净变化。null/undefined = 不可知。 */
  change: number | null | undefined
}

/** 千分位。与页面其余数字同一套写法。 */
function fmt(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

/**
 * 返回括号里那句话;没有可说的(不可知、或零变化)返回 null,调用方不渲染括号。
 *
 * 净空席位 `change < 0` = 净持仓更负 = 空头仓位变大 = 「净空+」。
 * 纯净数 "(−1万)" 会把这种情况读成「在减」,方向词就是为了堵这个歧义(DEC-149)。
 */
export function netChangeLabel(m: NetChange): string | null {
  const { net, change } = m
  if (change === null || change === undefined || change === 0) return null
  const mag = fmt(Math.abs(change))
  if (net > 0) return `净多${change > 0 ? '+' : '-'}${mag}`
  if (net < 0) return `净空${change < 0 ? '+' : '-'}${mag}`
  // 今天正好持平:没有「哪一边」可说,给纯净数并带上符号。
  return `净${change > 0 ? '+' : ''}${fmt(change)}`
}
