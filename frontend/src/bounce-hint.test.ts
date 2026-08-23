import { describe, expect, it } from 'vitest'
import { bounceHint, pairPosBand, pickBounceDay } from './bounce-hint'

describe('生猪卸仓反弹窗口 → 套利页文案', () => {
  const base = { min: 0.5, note: '' }

  it('低位 + 卸仓在参考区间内 → 窗口开,写出卸掉多少/区间/低位', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.15, side: 'net_short' }, 0.12)
    expect(h.on).toBe(true)
    expect(h.text).toContain('窗口开')
    expect(h.text).toContain('已卸掉 15%')
    expect(h.text).toContain('反弹参考区间 10~20%')
    expect(h.text).toContain('处于价差低位')
  })

  it('低位 + 已过参考区间 → 窗口关,说反弹多半已走完', () => {
    const h = bounceHint({ ...base, active: true, unload: 0.38, side: 'net_short' }, 0.2)
    expect(h.on).toBe(false)
    expect(h.text).toContain('已卸掉 38%')
    expect(h.text).toContain('已过反弹参考区间')
    expect(h.text).toContain('处于价差低位')
  })

  it('低位 + 未到参考区间 → 半开,说早了一点', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.05, side: 'net_short' }, 0.1)
    expect(h.on).toBe(false)
    expect(h.text).toContain('未到反弹参考区间')
  })

  it('高位不论卸多少 → 窗口关,牛市价差别追', () => {
    const h = bounceHint({ ...base, active: true, unload: 0.15, side: 'net_short' }, 0.95)
    expect(h.on).toBe(false)
    expect(h.text).toContain('处于价差高位')
    expect(h.text).toContain('别追')
  })

  it('位置未知(组合太新)只报卸仓与区间,不判高低位', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.15, side: 'net_short' }, null)
    expect(h.text).toContain('组合位置未知')
    expect(h.text).not.toContain('处于价差')
  })

  it('窗口关·机构根本没净空:要点明牛市价差无条件是逆势', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.1, side: 'net_long' }, 0.1)
    expect(h.on).toBe(false)
    expect(h.text).toContain('未净空')
    expect(h.text).toContain('逆势')
  })

  it('位置分档:<30% 低位、>70% 高位、其余中位', () => {
    expect(pairPosBand(0.1)).toBe('low')
    expect(pairPosBand(0.5)).toBe('mid')
    expect(pairPosBand(0.9)).toBe('high')
    expect(pairPosBand(null)).toBeNull()
  })

  it('掉榜看不清时不编数字', () => {
    const h = bounceHint({ ...base, active: false, unload: null, side: 'net_short' }, 0.5)
    expect(h.text).toContain('掉榜看不清')
    expect(h.text).not.toContain('NaN')
  })
})

describe('按所选交易日取窗口状态', () => {
  const hist = [
    { d: '2026-08-18', active: false, unload: 0.4, side: 'net_short' as const },
    { d: '2026-08-19', active: true, unload: 0.52, side: 'net_short' as const },
    { d: '2026-08-21', active: false, unload: 0.46, side: 'net_short' as const }
  ]

  it('选了哪天就给哪天,不给最新', () => {
    expect(pickBounceDay(hist, '2026-08-19')?.active).toBe(true)
    expect(pickBounceDay(hist, '2026-08-21')?.active).toBe(false)
  })

  it('所选日没有行(节假日/周末)时退到之前最近的一天', () => {
    expect(pickBounceDay(hist, '2026-08-20')?.d).toBe('2026-08-19')
  })

  it('早于历史起点:没有数据就是 null,不编', () => {
    expect(pickBounceDay(hist, '2026-08-01')).toBeNull()
  })
})

