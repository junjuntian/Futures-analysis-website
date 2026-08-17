import { describe, expect, it } from 'vitest'

import { driftTone, points, revertPct, revertTone } from './revert'
import type { SpreadRevertStats } from './api'

function stats(rate: string, hit = 1, n = 2): SpreadRevertStats {
  return {
    side: 'high',
    hit,
    n,
    rate,
    move_points: null,
    drift_points: null,
    days: null
  }
}

describe('revertPct', () => {
  it('四舍五入到整数百分比', () => {
    // 生产实例：JD2609/JD2701 在 2026-08-14 是 11/12 年曾跌破起点。
    expect(revertPct(stats('0.9167', 11, 12))).toBe('92%')
    expect(revertPct(stats('0.5385'))).toBe('54%')
    expect(revertPct(stats('1'))).toBe('100%')
    expect(revertPct(stats('0'))).toBe('0%')
  })

  it('比率不可用时给破折号，不给 0%', () => {
    // 「0%」看着像个结论，其实是没有数据——这是整块统计里最容易误导人的地方。
    expect(revertPct(stats('nonsense'))).toBe('—')
  })
})

describe('revertTone', () => {
  it('明显高于抛硬币算强，明显低于算弱', () => {
    expect(revertTone(stats('0.9167', 11, 12))).toBe('strong')
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

describe('points', () => {
  it('带符号取整', () => {
    expect(points('101.5')).toBe('+102')
    expect(points('-166')).toBe('-166')
    expect(points('0')).toBe('0')
  })

  it('缺失或不可解析时返回 null，由界面留空', () => {
    expect(points(null)).toBeNull()
    expect(points('nonsense')).toBeNull()
  })
})

describe('driftTone', () => {
  it('区分「持到期朝回归走」和「反向走」', () => {
    // JD2609/JD2701:持到期中位 +81 点,朝回归走。
    expect(driftTone('81')).toBe('with')
    // JD2612/JD2701:回归率 100% 但持到期中位 −166 点,方向反的——这正是
    // 单看回归率会踩的坑,所以必须能上色标出来。
    expect(driftTone('-166')).toBe('against')
  })

  it('零与缺失不给倾向', () => {
    expect(driftTone('0')).toBe('')
    expect(driftTone(null)).toBe('')
    expect(driftTone('nonsense')).toBe('')
  })
})
