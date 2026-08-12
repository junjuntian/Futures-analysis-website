import { describe, expect, it } from 'vitest'

import { addChange, sideDelta, signedChange } from './seatChange'

describe('席位增减量求和', () => {
  it('未知不是零——源没给增减量时，和必须是未知而不是 0', () => {
    // 这条是本模块存在的理由。旧写法 `(acc ?? 0) + (delta ?? 0)` 在这里返回 0，
    // 页面于是显示「没变化」，而真相是「不知道变了多少」。
    expect(addChange(undefined, null)).toBeNull()
    expect(addChange(5, null)).toBeNull()
    expect(addChange(null, 5)).toBeNull()
    expect(addChange(null, null)).toBeNull()
  })

  it('第一个真值直接采用，后续累加', () => {
    expect(addChange(undefined, -31)).toBe(-31)
    expect(addChange(-31, -13)).toBe(-44)
    expect(addChange(0, 7)).toBe(7)
  })

  it('未知一旦出现就传染到底，顺序无关', () => {
    const forward = [10, null, 20].reduce<number | null | undefined>(
      (acc, d) => addChange(acc, d),
      undefined
    )
    const backward = [20, null, 10].reduce<number | null | undefined>(
      (acc, d) => addChange(acc, d),
      undefined
    )
    expect(forward).toBeNull()
    expect(backward).toBeNull()
  })

  it('一侧没有行按 0 计，不算未知', () => {
    // AU2612 只上了空头榜：多头侧是 undefined，不该让整个品种的净变化变成未知。
    expect(sideDelta(undefined)).toBe(0)
    expect(sideDelta(null)).toBeNull()
    expect(sideDelta(-13)).toBe(-13)
  })

  it('未知与无行都渲染成空，正数带加号', () => {
    expect(signedChange(null)).toBe('')
    expect(signedChange(undefined)).toBe('')
    expect(signedChange(-31)).toBe('-31')
    expect(signedChange(31)).toBe('+31')
    expect(signedChange(0)).toBe('0')
  })

  it('净变化 = Σ多头 − Σ空头，未知照样吸收', () => {
    // 生产实例：高盛 2026-08-11 黄金，AU2610 多头 -31、AU2612 空头 -13。
    const lines = [
      { longChange: -31 as number | null | undefined, shortChange: undefined },
      { longChange: undefined, shortChange: -13 as number | null | undefined }
    ]
    const net = lines.reduce<number | null | undefined>((sum, line) => {
      const long = sideDelta(line.longChange)
      const short = sideDelta(line.shortChange)
      return addChange(addChange(sum, long), short === null ? null : -short)
    }, undefined)
    expect(net).toBe(-18) // 三禾同日显示「减少18」

    const withUnknown = [{ longChange: null, shortChange: undefined }].reduce<
      number | null | undefined
    >((sum, line) => {
      const long = sideDelta(line.longChange)
      const short = sideDelta(line.shortChange)
      return addChange(addChange(sum, long), short === null ? null : -short)
    }, undefined)
    expect(withUnknown).toBeNull()
  })
})
