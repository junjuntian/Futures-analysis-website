import { describe, expect, it } from 'vitest'

import { revertPct, revertTone } from './revert'
import type { SpreadRevertStats } from './api'

function stats(rate: string, hit = 1, n = 2): SpreadRevertStats {
  return { threshold: '0.03', side: 'low', hit, n, rate }
}

describe('revertPct', () => {
  it('四舍五入到整数百分比', () => {
    expect(revertPct(stats('0.5385'))).toBe('54%')
    expect(revertPct(stats('0.2941'))).toBe('29%')
    expect(revertPct(stats('1'))).toBe('100%')
    expect(revertPct(stats('0'))).toBe('0%')
  })

  it('比率不可用时给破折号，不给 0%', () => {
    // 「0%」看着像个结论，其实是没有数据——这一条是整块统计里最容易误导人的地方。
    expect(revertPct(stats('nonsense'))).toBe('—')
  })
})

describe('revertTone', () => {
  it('明显高于抛硬币算强，明显低于算弱', () => {
    // 生产实例：鸡蛋 JD2611/JD2612 低位 45/54 段回归 = 83%，
    // JD2609/JD2702 只有 15/37 = 41%——同一天同一个品种，差着一倍。
    expect(revertTone(stats('0.8333', 45, 54))).toBe('strong')
    expect(revertTone(stats('0.4054', 15, 37))).toBe('weak')
  })

  it('50% 上下五个百分点内算中性，不来回换色', () => {
    expect(revertTone(stats('0.50'))).toBe('')
    expect(revertTone(stats('0.54'))).toBe('')
    expect(revertTone(stats('0.46'))).toBe('')
    // 门槛本身算在强/弱里，边界不能悬空。
    expect(revertTone(stats('0.55'))).toBe('strong')
    expect(revertTone(stats('0.45'))).toBe('weak')
  })

  it('比率不可用时不给任何强弱暗示', () => {
    expect(revertTone(stats('nonsense'))).toBe('')
  })
})
