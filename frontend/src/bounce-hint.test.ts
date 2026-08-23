import { describe, expect, it } from 'vitest'
import { bounceHint, pickBounceDay } from './bounce-hint'

describe('生猪卸仓反弹窗口 → 套利页文案', () => {
  const base = { min: 0.5, note: '' }

  it('窗口开:要写出已卸掉多少与阈值,并提醒一个月内', () => {
    const h = bounceHint({ ...base, active: true, unload: 0.62, side: 'net_short' })
    expect(h.on).toBe(true)
    expect(h.text).toContain('窗口开')
    expect(h.text).toContain('62%')
    expect(h.text).toContain('≥50%')
    expect(h.text).toContain('一个月内')
  })

  it('窗口关·机构净空但没卸够:要说还差多少', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.46, side: 'net_short' })
    expect(h.on).toBe(false)
    expect(h.text).toContain('只卸掉 46%')
  })

  it('窗口关·机构根本没净空:要点明牛市价差无条件是逆势', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.1, side: 'net_long' })
    expect(h.on).toBe(false)
    expect(h.text).toContain('未净空')
    expect(h.text).toContain('逆势')
  })

  it('差一点点没到阈值时,不能四舍五入成「只卸掉 50%,未到 50%」', () => {
    const h = bounceHint({ ...base, active: false, unload: 0.4975, side: 'net_short' })
    expect(h.text).toContain('49.8%')
    expect(h.text).not.toContain('只卸掉 50%')
  })

  it('掉榜看不清时不编数字', () => {
    const h = bounceHint({ ...base, active: false, unload: null, side: 'net_short' })
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

