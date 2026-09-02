import { describe, expect, it } from 'vitest'

import { basisTone } from './basisTone'

describe('basisTone', () => {
  // 2026-09-02 生产数据,逐个对过 basis = 主力期货 − 现货。
  it('正 = 期货升水(prem)—— 生猪 11,740 − 10,970 = +770', () => {
    expect(basisTone(770)).toBe('prem')
    expect(basisTone(5)).toBe('prem') // 玻璃 961 − 956
  })

  it('负 = 期货贴水(disc)—— 纯碱 1,056 − 1,087 = −31', () => {
    expect(basisTone(-31)).toBe('disc')
    expect(basisTone(-1485)).toBe('disc') // 鸡蛋
  })

  /**
   * 这一条钉住的是**方向本身**,不是实现细节:页面此前把正基差标成「贴水」
   * 并按上涨色渲染,于是生猪 7% 的期货升水读起来成了利多。
   * 谁要再改这个映射,先回答:期货比现货贵,算升水还是贴水?
   */
  it('期货比现货贵一定是升水,不许再标成贴水', () => {
    const 期货 = 11740, 现货 = 10970
    expect(basisTone(期货 - 现货)).toBe('prem')
    expect(basisTone(期货 - 现货)).not.toBe('disc')
  })

  it('0 算升水侧(平水,不给相反的暗示)', () => {
    expect(basisTone(0)).toBe('prem')
  })

  it('取不到值就不上色', () => {
    expect(basisTone(null)).toBeNull()
    expect(basisTone(undefined)).toBeNull()
    expect(basisTone('')).toBeNull()
    expect(basisTone('abc')).toBeNull()
  })
})
