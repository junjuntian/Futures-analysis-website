/**
 * 总览页的两处判断逻辑。
 *
 * 单独成文件是为了能测。这两处错了都不会露馅：极值排序错了，首页照样列出三个
 * 组合，只是不是最该看的那三个；到齐判定错了，有缺口的日子会显示成全绿——而
 * 「数据到齐了吗」这张卡片的全部意义就在于回答这一件事。
 */

export interface MonitorTrackLike {
  position: string | null
  alert: string | null
}
export interface MonitorItemLike {
  pair: MonitorTrackLike
  years: MonitorTrackLike | null
  alert: string | null
}

/**
 * 触发组合按「离中线多远」排序，最远的在前。
 *
 * 位置 0.5 是区间正中,毫无看点;两端才要看。所以比的是 |位置 − 0.5|,不是位置
 * 本身——直接按位置排序会把所有低位组合排到最后,而低位和高位一样值得看。
 * 一个组合有当年与历年两条轨,取更极端的那条代表它。
 */
export function rankByExtremity<T extends MonitorItemLike>(items: T[], limit = 3): T[] {
  return items
    .filter((item) => item.alert !== null)
    .map((item) => {
      const positions = [item.pair.position, item.years?.position ?? null]
        .filter((value): value is string => value !== null)
        .map(Number)
        .filter((value) => Number.isFinite(value))
      const extremity = positions.length
        ? Math.max(...positions.map((value) => Math.abs(value - 0.5)))
        : 0
      return { item, extremity }
    })
    .sort((a, b) => b.extremity - a.extremity)
    .slice(0, limit)
    .map((entry) => entry.item)
}

export interface DayLike {
  trade_date: string
  exchanges: string[]
}

export interface DayCompleteness {
  trade_date: string
  complete: boolean
}

/**
 * 最近若干个交易日,席位与行情是否都覆盖了各自该有的交易所。
 *
 * **两个期望值必须分开传**(2026-09-01 修)。原来只有一个 `expectedCount`,
 * 取的是席位与行情的**并集**;而 INE(上期能源)**按设计只有行情没有席位**
 * (DEC-158:能源中心不披露原油席位排名)。于是并集是 5、席位永远只有 4,
 * 判定恒为 false ——「有缺口」从三品种上线那天起一直亮着,**一个永远为真的
 * 告警等于没有告警**,运营者 2026-09-01 问「这个有缺口是什么意思」才发现。
 *
 * @param seatExpected  席位该有几家
 * @param priceExpected 行情该有几家
 */
export function dayCompleteness(
  seats: DayLike[],
  prices: DayLike[],
  seatExpected: number,
  priceExpected: number,
  limit = 10
): DayCompleteness[] {
  return seats.slice(0, limit).map((day) => {
    const price = prices.find((item) => item.trade_date === day.trade_date)
    // 期望为 0 表示「一家都没见过」,那是没有数据,不是全部到齐。
    // 少了这个判断,空库会显示成一片绿。
    const complete =
      seatExpected > 0 &&
      priceExpected > 0 &&
      day.exchanges.length === seatExpected &&
      price?.exchanges.length === priceExpected
    return { trade_date: day.trade_date, complete }
  })
}
