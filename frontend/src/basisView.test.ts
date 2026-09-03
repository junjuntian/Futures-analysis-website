import { describe, expect, it } from 'vitest'

import { basisConventional, basisPercentile, basisTone } from './basisView'

/**
 * 数字全部来自 2026-09-02 生产库(库里原值 = 期货 − 现货)与运营者同花顺屏幕
 * (惯例口径 = 现货 − 期货)。两边同一天、同一合约,可以逐个对。
 */
describe('basisView', () => {
  it('转成惯例口径后与同花顺屏幕同号', () => {
    // 焦煤:库里 1,664 − 2,349 = −685;同花顺 2,054 − 1,686.5 = +367.5
    // 数值不同(现货基准地不同),**符号必须同**。
    expect(basisConventional(-685)).toBe(685)
    expect(Math.sign(basisConventional(-685)!)).toBe(Math.sign(367.5))
    // 纯碱:库里 1,056 − 1,087 = −31 → 惯例 +31;同花顺 998 − 1,068 = −70。
    // **这一对符号仍然相反** —— 那是现货基准差造成的,不是口径转换的错,
    // 所以这条测试只钉转换本身,不钉与同花顺同号。
    expect(basisConventional(-31)).toBe(31)
  })

  it('惯例口径:正 = 期货贴水(disc),负 = 期货升水(prem)', () => {
    // 生猪库里 +770(期货比现货贵)→ 惯例 −770 → 升水
    expect(basisTone(770)).toBe('prem')
    // 焦煤库里 −685(现货比期货贵)→ 惯例 +685 → 贴水
    expect(basisTone(-685)).toBe('disc')
    expect(basisTone(-1485)).toBe('disc') // 鸡蛋
    expect(basisTone(5)).toBe('prem')     // 玻璃,期货略贵
  })

  /**
   * 2026-09-03 我一天之内把这个方向标反过两次:第一次是页面原文把口径写反,
   * 第二次是我「修」的时候只对齐了库里原值、没对齐行业惯例。这条正面钉死。
   */
  it('期货比现货贵 = 升水,现货比期货贵 = 贴水,不许再反', () => {
    const 期货 = 11740, 现货 = 10970
    expect(basisTone(期货 - 现货)).toBe('prem')   // 期货贵 → 升水
    expect(basisTone(现货 - 期货)).toBe('disc')   // 现货贵 → 贴水
  })

  it('分位跟着翻 —— 只翻数不翻分位比不翻更糟', () => {
    expect(basisPercentile(0.61)).toBeCloseTo(0.39, 10)
    expect(basisPercentile(0)).toBe(1)
    expect(basisPercentile(1)).toBe(0)
  })

  it('取不到值就不显示、不上色', () => {
    for (const bad of [null, undefined, '', 'abc']) {
      expect(basisConventional(bad as never)).toBeNull()
      expect(basisTone(bad as never)).toBeNull()
      expect(basisPercentile(bad as never)).toBeNull()
    }
  })
})
