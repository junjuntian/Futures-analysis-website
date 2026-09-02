import { describe, expect, it } from 'vitest'

import { netChangeLabel } from './netChangeLabel'

describe('netChangeLabel', () => {
  it('按今天在哪一边说净持仓的变化', () => {
    expect(netChangeLabel({ net: -27933, change: 90187 })).toBe('净空-90,187')
    expect(netChangeLabel({ net: 33963, change: -9258 })).toBe('净多-9,258')
    expect(netChangeLabel({ net: -92981, change: -442 })).toBe('净空+442')
    expect(netChangeLabel({ net: 21610, change: -7458 })).toBe('净多-7,458')
  })

  it('净空席位加空 = 「净空+」,不是「净空-」', () => {
    // change<0 表示净持仓更负,即空头仓位变大。纯净数会被读反,这正是加方向词的理由。
    expect(netChangeLabel({ net: -12454, change: -3018 })).toBe('净空+3,018')
  })

  /**
   * 2026-09-02 我一度给跨零加了一支「由净空 A 转净多 B」,**运营者否了**:
   * 「东证还是按原来显示,那样显示没有错」。这一条把否掉的结论钉住,
   * 免得后来者(包括我自己)看着觉得不严谨又改回去。
   */
  it('跨零仍按今天这一边说 —— 运营者拍板,不要再改成「由净空…转净多…」', () => {
    const label = netChangeLabel({ net: 39666, change: 100239 })
    expect(label).toBe('净多+100,239')
    expect(label).not.toContain('转')
  })

  it('不可知与零变化都不渲染括号', () => {
    expect(netChangeLabel({ net: 100, change: null })).toBeNull()
    expect(netChangeLabel({ net: 100, change: undefined })).toBeNull()
    expect(netChangeLabel({ net: 100, change: 0 })).toBeNull()
  })

  it('今天正好持平时没有「哪一边」可说', () => {
    expect(netChangeLabel({ net: 0, change: 500 })).toBe('净+500')
    expect(netChangeLabel({ net: 0, change: -500 })).toBe('净-500')
  })
})
