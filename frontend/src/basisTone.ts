/**
 * 主力基差的方向与配色。
 *
 * **口径钉死:`basis = 主力期货 − 现货`。正 = 期货升水,负 = 期货贴水。**
 * 这不是约定俗成的猜测,是拿三个品种逐个对过的(2026-09-02 生产数据):
 *
 *     生猪 11,740 − 10,970 = +770      玻璃 961 − 956 = +5
 *     纯碱 1,056 − 1,087 = −31
 *
 * 上游是生意社经 akshare 的 `dom_basis`,库里原样存,没有取过负号。
 *
 * ## 为什么单独提出来
 *
 * 页面上那句解释**原来是反的** —— 写的是「基差 = 现货 − 主力期货,为正是期货
 * 贴水」,颜色也跟着标反:生猪 +770 明明是期货升水 7%,页面按「贴水」上色,
 * 读起来成了利多。**一个方向词标反,比数字算错更难发现** ——
 * 数字对得上,话是反的(同 DEC-179 那次)。
 *
 * 埋在 `<script setup>` 的模板表达式里测不到,所以提出来。
 */
export type BasisTone = 'prem' | 'disc'

/**
 * `prem` = 期货升水(期货更贵,有向现货回归的下行压力)→ 页面用下跌色;
 * `disc` = 期货贴水(现货更贵,有向现货修复的上行空间)→ 页面用上涨色。
 */
export function basisTone(basis: number | string | null | undefined): BasisTone | null {
  if (basis === null || basis === undefined || basis === '') return null
  const value = Number(basis)
  if (!Number.isFinite(value)) return null
  return value >= 0 ? 'prem' : 'disc'
}
