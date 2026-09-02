/**
 * 席位净持仓变化的方向文案(「净多+X」/「净空+X」)。
 *
 * 提成独立模块只为一件事:**能被测试直接调到**。埋在 `<script setup>` 里的函数
 * 导不出来(同 shelf.ts / seatChange.ts),而这段逻辑刚出过一次线上错 ——
 * 不是崩溃,是**安静地说了一句不成立的话**。
 *
 * ## 2026-09-02 运营者指出的那个错
 *
 * 玻璃页组内各家显示:
 *
 *     东证期货  39,666 手 (净多+100,239)
 *
 * 数字本身与库里逐笔对得上(08-26 净空 60,573 → 09-02 净多 39,666,净变化
 * +100,239)。**错的是那句话**:东证的多头从来没有增加过 100,239 手 ——
 * 它是**从净空翻到了净多**,跨过了 0。
 *
 * 原实现只看**今天在哪一边**,然后把整个净变化安到那一边上:
 *
 *     if (net > 0) return `净多${change > 0 ? '+' : '-'}${|change|}`
 *
 * 席位在窗口内没换边时这么说是对的(中信一直净空,「净空-90,187」成立);
 * **一旦跨零,这句话就不再成立** —— 净变化里有一段是在对面那一侧走完的。
 *
 * 所以跨零单独说一句「由净空 A 转净多 B」:两个端点都是真实持仓,读者自己
 * 就能看出跨了边,也不需要再去做减法。
 */
export interface NetChange {
  /** 今日净持仓。正=净多,负=净空。 */
  net: number
  /** 窗口内的净变化(今日 − 窗口起点)。null/undefined = 不可知。 */
  change: number | null | undefined
}

/** 千分位。与页面其余数字同一套写法。 */
function fmt(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

/**
 * 返回括号里那句话;没有可说的(不可知、或零变化)返回 null,调用方不渲染括号。
 *
 * **不要把跨零那支合并回去**:合并的写法看着简洁,代价是说一句假话。
 */
export function netChangeLabel(m: NetChange): string | null {
  const { net, change } = m
  if (change === null || change === undefined || change === 0) return null
  const was = net - change
  // 跨零:两侧不是同一件事,不能拿净变化去说单侧的增减。
  if (net > 0 && was < 0) return `由净空 ${fmt(-was)} 转净多 ${fmt(net)}`
  if (net < 0 && was > 0) return `由净多 ${fmt(was)} 转净空 ${fmt(-net)}`
  const mag = fmt(Math.abs(change))
  // 同一侧:按今天这一边说增减。净空席位 change<0 = 空头仓位变大 = 「净空+」。
  if (net > 0) return `净多${change > 0 ? '+' : '-'}${mag}`
  if (net < 0) return `净空${change < 0 ? '+' : '-'}${mag}`
  // 今天正好持平:没有「哪一边」可说,给纯净数并带上符号。
  return `净${change > 0 ? '+' : ''}${fmt(change)}`
}
