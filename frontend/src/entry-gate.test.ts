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

describe('结论以引擎为准', () => {
  /**
   * 引擎知道做多开关、回撤要求、交割窗口 —— 这些这个模块看不到。
   * 它自己推一遍就会与引擎打架,那正是 DEC-104 的病根。
   */
  const withEngine = (over: Partial<EntryGateInput['signal']>): EntryGateInput => ({
    signal: { z: 2.09, enter: 1.0, ...over },
    retail: { z: 1.19, resonate: true, trades: true }
  })

  it('引擎给了方向就报已达标', () => {
    expect(entryGateText(withEngine({ entry_side: 'long' }))).toContain('已达标')
  })

  it('引擎说被挡住就照它的话说,哪怕本地判据算出达标', () => {
    // 散户 1.19 ≥ 1,本地判据会说"达标";但引擎知道做多关着。
    const text = entryGateText(withEngine({ entry_side: null, entry_blocked: '本品种做多已关' }))
    expect(text).toContain('本品种做多已关')
    expect(text).not.toContain('已达标')
  })

  it('引擎没给这两个字段时退回本地判据(老 payload 不炸)', () => {
    expect(entryGateText(withEngine({}))).toContain('已达标')
  })
})

describe('成本进场(DEC-112,鸡蛋)', () => {
  const base = {
    rules: { signal_source: 'cost' },
    signal: { z: 0.4, enter: 1, entry_side: null as 'long' | 'short' | null,
              entry_blocked: '价 3702 高于机构成本 3660,等回到成本再进' },
    retail: { z: 0.9, resonate: true, trades: true }
  }

  it('结论全由引擎给:被挡时转述 entry_blocked,不摆 z 对门槛', () => {
    const g = entryGate(base)
    expect(g.source).toBe('cost')
    expect(g.met).toBe(false)
    const text = entryGateText(base)
    expect(text).toContain('高于机构成本 3660')
    // 绝不能出现「需达 1」这类 z 门槛话术 —— 那是 DEC-104 修过的误会
    expect(text).not.toContain('需达')
  })

  it('entry_side 有值即已达标', () => {
    const d = { ...base, signal: { ...base.signal, entry_side: 'long' as const, entry_blocked: null } }
    expect(entryGate(d).met).toBe(true)
    expect(entryGateText(d)).toContain('次日开盘进场')
  })

  it('没带 signal_source 的老 payload 走原路径,行为不变', () => {
    const d = { ...base, rules: {} }
    expect(entryGate(d).source).toBe('retail')
  })
})

