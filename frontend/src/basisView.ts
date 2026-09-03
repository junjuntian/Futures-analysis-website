/**
 * 主力基差的**显示口径**统一到行业惯例:`基差 = 现货 − 期货`。
 *
 * ## 为什么要转一道
 *
 * 库里存的是上游(生意社经 akshare 的 `dom_basis`)的原值,口径是
 * **`期货 − 现货`**,与惯例**符号相反**。2026-09-02 生产数据逐个验过:
 *
 *     生猪 11,740 − 10,970 = +770     玻璃 961 − 956 = +5
 *     纯碱 1,056 − 1,087 = −31        焦煤 1,664 − 2,349 = −685
 *
 * 而运营者每天看的同花顺期货通用的是惯例口径(拿它自己屏幕上的数验过):
 *
 *     焦煤 现货 2,054 − 期货 1,686.5 = **+367.5**
 *     鸡蛋 现货 4,850 − 期货 3,691   = **+1,159**
 *     纯碱 现货   998 − 期货 1,068   = **−70**
 *
 * **两边符号相反,同一件事读出来是两个方向。** 所以显示层统一取负,
 * 让页面上的数与他手边那块屏幕**同号可比**。
 *
 * ## 分位也要跟着翻
 *
 * `percentile` 是在**原序列**(期货−现货)上算的。取负之后大小关系整个颠倒,
 * 分位必须变成 `1 − p`,否则会出现「基差是正的(贴水),分位却说它在最贵那一档」
 * 这种自相矛盾。**这是本次最容易漏的一处** —— 只翻数不翻分位,比不翻更糟。
 */

/** `disc` = 期货贴水(现货更贵,期货有向上修复的空间)→ 上涨色;
 *  `prem` = 期货升水(期货更贵,有向现货回归的下行压力)→ 下跌色。 */
export type BasisTone = 'prem' | 'disc'

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** 库里原值(期货−现货)→ 惯例口径(现货−期货)。取不到就 null。 */
export function basisConventional(raw: number | string | null | undefined): number | null {
  const n = toNumber(raw)
  return n === null ? null : -n
}

/**
 * 配色。**入参是库里的原值**,函数内部自己转口径 —— 调用方不该记得要不要取负,
 * 那正是 2026-09-03 标反的原因。
 */
export function basisTone(raw: number | string | null | undefined): BasisTone | null {
  const conv = basisConventional(raw)
  if (conv === null) return null
  // 惯例口径下:正 = 现货更贵 = 期货贴水。
  return conv >= 0 ? 'disc' : 'prem'
}

/** 分位跟着翻:原序列取负之后,第 p 分位变成第 1−p 分位。 */
export function basisPercentile(raw: number | string | null | undefined): number | null {
  const p = toNumber(raw)
  if (p === null) return null
  return 1 - p
}
