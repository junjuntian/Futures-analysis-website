import { describe, expect, it } from 'vitest'
import { bounceHint } from './bounce-hint'

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

  it('掉榜看不清时不编数字', () => {
    const h = bounceHint({ ...base, active: false, unload: null, side: 'net_short' })
    expect(h.text).toContain('掉榜看不清')
    expect(h.text).not.toContain('NaN')
  })
})
