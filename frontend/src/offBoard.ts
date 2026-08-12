/**
 * 掉榜区间：把连续的「持仓未知」日并成区间。
 *
 * 交易所只公布持仓前 20 名。某席位掉出榜单的那些天，官方文件里根本没有他这一行——
 * 那是**不知道**，不是**零**。曲线在这些天必须断开，但光断开看不出是缺数据还是
 * 真的平仓了，所以还要用底色把区间圈出来。
 *
 * 单独成文件是为了能测：区间合并是典型的差一格逻辑，画在图上错一天看不出来。
 */

/** ECharts markArea 的一段：起止两个 x 轴刻度。 */
export type Band = [{ xAxis: string }, { xAxis: string }]

/**
 * @param days 按日期升序排列的序列。`known` 为 false 表示那天持仓未知。
 */
export function offBoardBands(days: Array<{ trade_date: string; known: boolean }>): Band[] {
  const bands: Band[] = []
  let start: string | null = null
  let previous: string | null = null

  for (const day of days) {
    if (!day.known) {
      if (start === null) start = day.trade_date
    } else if (start !== null) {
      // 区间在**上一个**未知日结束，不是今天——今天他已经回榜了。
      bands.push([{ xAxis: start }, { xAxis: previous as string }])
      start = null
    }
    if (!day.known) previous = day.trade_date
  }
  // 序列以未知日收尾：区间一直开到最后一天。
  if (start !== null) bands.push([{ xAxis: start }, { xAxis: previous as string }])
  return bands
}
