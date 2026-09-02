import { describe, expect, it } from 'vitest'

import { netChangeLabel } from './netChangeLabel'

describe('netChangeLabel', () => {
  // 2026-09-02 运营者指出的那一格:玻璃页东证期货。
  // 库里逐笔:08-26 净空 60,573 → 09-02 净多 39,666,净变化 +100,239。
  it('跨零时不说「净多+100,239」—— 东证的多头从没增加过那么多', () => {
    const label = netChangeLabel({ net: 39666, change: 100239 })
    expect(label).toBe('由净空 60,573 转净多 39,666')
    expect(label).not.toContain('100,239')
  })

  it('反向跨零同理', () => {
    expect(netChangeLabel({ net: -39666, change: -100239 }))
      .toBe('由净多 60,573 转净空 39,666')
  })

  // 同一张卡上的中信:一直净空,只是空头小了。这一句原来就是对的,别改坏。
  it('没换边时照旧按今天这一侧说增减', () => {
    expect(netChangeLabel({ net: -27933, change: 90187 })).toBe('净空-90,187')
    expect(netChangeLabel({ net: 33963, change: -9258 })).toBe('净多-9,258')
    expect(netChangeLabel({ net: -92981, change: -442 })).toBe('净空+442')
    expect(netChangeLabel({ net: 21610, change: -7458 })).toBe('净多-7,458')
  })

  it('净空席位加空 = 「净空+」,不是「净空-」', () => {
    // change<0 表示净持仓更负,即空头仓位变大。纯净数会被读反,这正是加方向词的理由。
    expect(netChangeLabel({ net: -12454, change: -3018 })).toBe('净空+3,018')
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

  it('从 0 出发不算跨零 —— 起点没有对面那一侧', () => {
    expect(netChangeLabel({ net: 500, change: 500 })).toBe('净多+500')
    expect(netChangeLabel({ net: -500, change: -500 })).toBe('净空+500')
  })
})
