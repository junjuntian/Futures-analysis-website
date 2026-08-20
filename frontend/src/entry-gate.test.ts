import { describe, expect, it } from 'vitest'

import { entryGate, entryGateText, type EntryGateInput } from './entry-gate'

/** 玻璃 FG @2026-08-20 的真实取值。 */
function fg(over: Partial<EntryGateInput['retail']> = {}, flowZ = 2.09): EntryGateInput {
  return {
    signal: { z: flowZ, enter: 1.0 },
    retail: { z: 0.92, resonate: true, trades: true, ...over }
  }
}

describe('entryGate', () => {
  it('方案 C 下比的是散户那一路,不是机构', () => {
    // 运营者当天看到的:机构 2.09 > 1,页面却显示无持仓。
    // 真正被测的是散户 0.92,差 0.08 没到线。
    const g = entryGate(fg())
    expect(g.source).toBe('retail')
    expect(g.value).toBe(0.92)
    expect(g.met).toBe(false)
  })

  it('背离时直接判死,再大也不进场', () => {
    // 共振不成立时,散户那个数无论多大都不该被拿来说事。
    const g = entryGate(fg({ z: 3.5, resonate: false }))
    expect(g.divergent).toBe(true)
    expect(g.met).toBe(false)
    expect(g.value).toBeNull()
  })

  it('散户信号到线才算达标', () => {
    expect(entryGate(fg({ z: 1.0 })).met).toBe(true)
    expect(entryGate(fg({ z: 0.99 })).met).toBe(false)
  })

  it('做空方向对称 —— 比的是绝对值', () => {
    // 引擎的判据是 ze <= -enter(做空)或 ze >= enter(做多),两边同一个量级。
    expect(entryGate(fg({ z: -1.4 })).met).toBe(true)
    expect(entryGate(fg({ z: -0.4 })).met).toBe(false)
  })

  it('不走方案 C 时退回比机构信号', () => {
    // signal_source = "flow" 的品种,原来的显示本来就是对的,不能一起改坏。
    const g = entryGate(fg({ trades: false, resonate: false }))
    expect(g.source).toBe('flow')
    expect(g.value).toBe(2.09)
    expect(g.met).toBe(true)
  })

  it('数缺失时不谎报达标', () => {
    // 预热期、掉榜日都可能取不到数。取不到就是取不到,不能当成没达标之外的第三态,
    // 更不能因为「另一路有数」就拿另一路顶上。
    expect(entryGate(fg({ z: null })).met).toBe(false)
    const noFlow: EntryGateInput = {
      signal: { z: null, enter: 1.0 },
      retail: { z: 0.92, resonate: true, trades: false }
    }
    expect(entryGate(noFlow).met).toBe(false)
    expect(entryGateText(noFlow)).toContain('—')
  })
})

describe('entryGateText', () => {
  it('点名比的是哪一路,并说清机构那个数不是它', () => {
    const text = entryGateText(fg())
    expect(text).toContain('共振后的散户信号需达 1')
    expect(text).toContain('0.92')
    // **关键**:2.09 就摆在旁边的卡片里,不说清楚读者一定会拿它去对门槛。
    expect(text).toContain('2.09')
    expect(text).toContain('不是这里比的那个数')
  })

  it('达标时说清是次日开盘进场,不是当场', () => {
    // DEC-090:信号日收盘出信号,次日开盘成交。达标当天仍然显示无持仓是对的。
    expect(entryGateText(fg({ z: 1.6 }))).toContain('次日开盘进场')
  })

  it('背离时不报数字,只说背离', () => {
    const text = entryGateText(fg({ z: 3.5, resonate: false }))
    expect(text).toContain('背离')
    expect(text).not.toContain('需达')
  })

  it('不走方案 C 时文案回到机构口径', () => {
    expect(entryGateText(fg({ trades: false }))).toContain('机构合计流向需达 1')
  })
})
