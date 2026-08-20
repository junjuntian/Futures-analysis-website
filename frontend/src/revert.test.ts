import { describe, expect, it } from 'vitest'

import {
  driftTone,
  isChoppy,
  isDecayZone,
  isQualified,
  isRedLine,
  points,
  revertPct,
  revertTone,
  tradeDirection
} from './revert'
import type { SpreadRevertStats } from './api'

function stats(rate: string, hit = 1, n = 2): SpreadRevertStats {
  return {
    side: 'high',
    hit,
    n,
    rate,
    move_points: null,
    drift_points: null,
    mae_points: null,
    mae_max_points: null,
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

describe('isQualified', () => {
  function full(rate: string, drift: string | null): SpreadRevertStats {
    return {
      side: 'high',
      hit: 10,
      n: 12,
      rate,
      move_points: '100',
      drift_points: drift,
      mae_points: '62',
      mae_max_points: '247',
      days: 30
    }
  }

  it('只看持到期为正 —— 触及率那条已去掉(DEC-098)', () => {
    // 生产实例：JD2609/JD2701 高位 92%、持到期 +81 —— 合格。
    expect(isQualified(full('0.9167', '81'))).toBe(true)
    // JD2612/JD2701:回归率 100% 但持到期 −166 —— 这正是要拦的陷阱。
    expect(isQualified(full('1', '-166'))).toBe(false)
    // **触及率低但持到期为正,现在算合格**:那条判据是「窗口内曾经朝这边动过一次」,
    // 全表 84% 的行两侧都过它、41% 两侧同时 100%,它筛不掉任何东西。
    expect(isQualified(full('0.44', '120'))).toBe(true)
    // 触及率不管多高,持到期不为正就是不合格 —— 判据只剩这一条。
    expect(isQualified(full('1', '0'))).toBe(false)
    expect(isQualified(full('0.13', '1'))).toBe(true)
  })

  it('持到期缺失按不合格 —— 资格要正证据,「不知道」不放行', () => {
    expect(isQualified(full('1', null))).toBe(false)
  })
})

describe('isChoppy', () => {
  it('第 2 次及以后的穿线算信号差', () => {
    // JM2609/JM2701 生产序列:08-04 第 1 次(干净)、08-06 第 2 次、08-13 第 3 次。
    expect(isChoppy(1)).toBe(false)
    expect(isChoppy(2)).toBe(true)
    expect(isChoppy(3)).toBe(true)
  })

  it('没拐头(null)谈不上拐头质量', () => {
    expect(isChoppy(null)).toBe(false)
    expect(isChoppy(0)).toBe(false)
  })
})

describe('交割红线与衰减区', () => {
  it('剩余 ≤15 交易日进红线,15~40 是衰减区,40 以上正常', () => {
    // 留一法数据:<15 日段持到底中位 −21.7%、15~40 日 −32.5%、>40 日 +54.8%。
    expect(isRedLine(10)).toBe(true)
    expect(isRedLine(15)).toBe(true)
    expect(isRedLine(16)).toBe(false)
    expect(isDecayZone(16)).toBe(true)
    expect(isDecayZone(39)).toBe(true)
    expect(isDecayZone(40)).toBe(false)
    expect(isRedLine(93)).toBe(false)
    expect(isDecayZone(93)).toBe(false)
  })

  it('剩余天数未知时两者都不判 —— 判不了就不标', () => {
    expect(isRedLine(null)).toBe(false)
    expect(isDecayZone(null)).toBe(false)
  })
})

describe('tradeDirection', () => {
  it('高位侧是做空价差、低位侧是做多价差', () => {
    // 价差贴顶赌它往下 = 卖腿1买腿2;贴底赌它往上 = 买腿1卖腿2。
    expect(tradeDirection({ ...stats('1'), side: 'high' })).toBe('做空')
    expect(tradeDirection({ ...stats('1'), side: 'low' })).toBe('做多')
  })

  it('JM2612−JM2705 @2026-08-06:合格标与进场标必须指同一个方向', () => {
    // 那天报警侧是低位(持到期 +45,合格)、拐头侧是高位(持到期 −45)。
    // 修好之后统计跟拐头侧走,两个标都念「做空」,而做空侧判不合格 —— ⚡ 该灭。
    const turnSide = { ...stats('1', 13, 13), side: 'high', drift_points: '-45' }
    expect(tradeDirection(turnSide)).toBe('做空')
    expect(isQualified(turnSide)).toBe(false)
    // 反向那侧(界面单列出来对照)确实是笔合格的做多 —— 事后价差涨了 83.5 点。
    const altSide = { ...stats('1', 13, 13), side: 'low', drift_points: '45' }
    expect(tradeDirection(altSide)).toBe('做多')
    expect(isQualified(altSide)).toBe(true)
  })
})
