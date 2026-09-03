import { describe, expect, it } from 'vitest'
import { netLots, rollPressureHint, type RollPressureState } from './roll-pressure-hint'

// 被迫方近月 LH2609 正在被赶出场,对手腿是接任的 LH2611(DEC-189)。
const base: RollPressureState = {
  active: true, entry_flag: true, suppress_long: true,
  criterion: true, forced: true, anchor_date: '2026-08-03', retail_net_now: 9100,
  main: 'LH2609', next: 'LH2611', days_left: 14, window: 30,
  retail_net: 12000, hist_q1: 2936, hist_med: 4474, hist_q3: 8430,
  level: 'high', vol_ratio: 1.1, spread_now: -335, anchor: 20,
  history: [], note: '机制说明在引擎里'
}

describe('rollPressureHint(DEC-136/137/189)', () => {
  it('被迫方近月处历届高位才亮,并点名空近多次月', () => {
    const h = rollPressureHint(base)
    expect(h.on).toBe(true)
    expect(h.text).toContain('空 LH2609 多 LH2611')
    expect(h.text).toContain('被迫方 LH2609')
    expect(h.text).toContain('12,000')
  })
  it('净剩仓必须写出多空方向 —— 只印数字等于把信号的一半吞掉', () => {
    // 2026-09-03 运营者:「这净剩仓 7,686 手,多单还是空单,写清楚」。
    // 净多压近月(⚡做空价差)、净空托近月(⚡做多价差),方向是信号的一部分。
    expect(netLots(7686)).toBe('净多 7,686 手')
    expect(netLots(-23929)).toBe('净空 23,929 手')
    expect(netLots(0)).toBe('净持平')
    expect(netLots(null)).toBe('—')
    const h = rollPressureHint(base)
    expect(h.text).toContain('散户净多 12,000 手')
    expect(h.text).toContain('今 净多 9,100 手')
    expect(h.text).not.toMatch(/净剩仓 [\d,]+ 手/)
  })
  it('历届四分位跨零时,两端各写各的方向', () => {
    // 鸡蛋历届锚点有净空的届(JD2608 −23,929),Q1 为负、Q3 为正时
    // 写成「1,164~8,419」会让人以为都是净多。
    const h = rollPressureHint({ ...base, hist_q1: -5431, hist_q3: 8419 })
    expect(h.text).toContain('净空 5,431 手 ~ 净多 8,419 手')
    // 同向就只写一次方向,别啰嗦。
    const same = rollPressureHint({ ...base, hist_q1: 1164, hist_q3: 8419 })
    expect(same.text).toContain('净多 1,164~8,419 手')
  })
  it('判据读的是锚点日那一格,页面必须写明是哪天的数', () => {
    // 判据值(锚点日)和今值不是一个数。只印一个、又不说是哪天的,
    // 读的人拿它去和净持仓页对,永远对不上(DEC-179 同族)。
    const h = rollPressureHint(base)
    expect(h.text).toContain('锚点日(2026-08-03)')
    expect(h.text).toContain('今 净多 9,100 手')
  })
  it('没有被迫方时说清判据还没起算,不许说成「展示级」', () => {
    // 生猪剩 30~21 日这一段:表已经在显示,但判据只看被迫方近月剩 ≤20 那一段。
    // DEC-189 之前这一段是会亮 ⚡ 的,对齐回测口径后归零。
    const h = rollPressureHint({
      ...base, main: 'LH2611', days_left: 28, forced: false, anchor_date: null,
      entry_flag: false, suppress_long: false
    })
    expect(h.on).toBe(true)
    expect(h.text).not.toContain('展示级')
    expect(h.text).not.toContain('被迫方 LH2611'), '没有被迫方就不许给主力冠这个名'
    expect(h.text).not.toContain('锚点日'), '没有被迫方时判据值就是今天的主力剩仓'
    expect(h.text).toContain('剩 ≤20 日那一段,还没到')
  })
  it('展示级品种(焦煤)高位亮但不给 ⚡ 进场话术', () => {
    // 引擎 criterion=False 时 level 照标 high、entry_flag 恒假(REPORT_JM_THREE_GAPS_v1)
    const h = rollPressureHint({
      ...base, main: 'JM2701', next: 'JM2705', criterion: false,
      entry_flag: false, suppress_long: false
    })
    expect(h.on).toBe(true)
    expect(h.text).toContain('展示级')
    expect(h.text).not.toContain('⚡')
  })
  it('已经是被迫方却没亮,不许赖到窗口头上', () => {
    // 净剩仓不为正之类的原因,与窗口无关;乱归因比不说更坏。
    const h = rollPressureHint({ ...base, entry_flag: false, suppress_long: false })
    expect(h.on).toBe(true)
    expect(h.text).not.toContain('展示级')
    expect(h.text).not.toContain('还没到')
  })
  it('老 JSON 没有这几个键时什么都不补', () => {
    // 前端先于引擎上线的空窗期(DEC-089):猜错方向就是那句假话。
    const old = { ...base, entry_flag: false, suppress_long: false }
    delete old.criterion
    delete old.forced
    delete old.anchor_date
    delete old.retail_net_now
    const h = rollPressureHint(old)
    expect(h.on).toBe(true)
    expect(h.text).not.toContain('展示级')
    expect(h.text).not.toContain('还没到')
    expect(h.text).toContain('散户净多 12,000 手')
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
