import { describe, expect, it } from 'vitest'
import { rollPressureHint, type RollPressureState } from './roll-pressure-hint'

const base: RollPressureState = {
  active: true, entry_flag: true, suppress_long: true,
  main: 'LH2611', next: 'LH2701', days_left: 20, window: 30,
  retail_net: 12000, hist_q1: 2936, hist_med: 4474, hist_q3: 8430,
  level: 'high', vol_ratio: 1.1, spread_now: -335, anchor: 20,
  history: [], note: '机制说明在引擎里'
}

describe('rollPressureHint(DEC-136)', () => {
  it('窗口内且散户剩仓处历届高位才亮,并点名空近多次月', () => {
    const h = rollPressureHint(base)
    expect(h.on).toBe(true)
    expect(h.text).toContain('空 LH2611 多 LH2701')
    expect(h.text).toContain('12,000')
  })
  it('展示级品种(焦煤)高位亮但不给 ⚡ 进场话术', () => {
    // 引擎 criterion=False 时 level 照标 high、entry_flag 恒假(REPORT_JM_THREE_GAPS_v1)
    const h = rollPressureHint({ ...base, main: 'JM2701', next: 'JM2705', entry_flag: false, suppress_long: false })
    expect(h.on).toBe(true)
    expect(h.text).toContain('展示级')
    expect(h.text).not.toContain('⚡')
  })
  it('未到窗口不亮,写清还剩几天', () => {
    const h = rollPressureHint({ ...base, active: false, days_left: 53 })
    expect(h.on).toBe(false)
    expect(h.text).toContain('剩 53 个交易日')
  })
  it('低剩仓不亮且明说压力不明显 —— 历届低剩仓届价差常反涨', () => {
    const h = rollPressureHint({ ...base, level: 'low', retail_net: 500 })
    expect(h.on).toBe(false)
    expect(h.text).toContain('压力不明显')
  })
})
