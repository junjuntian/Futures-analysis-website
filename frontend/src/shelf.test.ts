import { describe, expect, it } from 'vitest'

import type { SpreadMonitorItem, SpreadShelf } from './api'
import { edgeOf, offsetText, shelfLabel } from './shelf'

function shelf(level: number, offset: number, extra: Partial<SpreadShelf> = {}): SpreadShelf {
  return {
    level: String(level),
    lo: String(level),
    hi: String(level),
    touches: 2,
    offset: String(offset),
    z: null,
    reach_pct: null,
    role: '',
    ...extra
  }
}

/** 只带阶梯的最小行——edgeOf 只读 shelves 与 spread。 */
function row(shelves: SpreadShelf[], spread = '-935'): SpreadMonitorItem {
  return { spread, shelves } as unknown as SpreadMonitorItem
}

describe('shelfLabel', () => {
  it('跨度小的报单点，跨度大的报区间', () => {
    // 并档阈值是 0.5σ；跨到 30 点就已经吃掉大半个日波动，再报中点就是骗人。
    expect(shelfLabel(shelf(-1355, -420))).toBe('-1355')
    const wide = { ...shelf(-1117, -182), lo: '-1155', hi: '-1080' }
    expect(shelfLabel(wide)).toBe('-1155~-1080')
  })
})

describe('offsetText', () => {
  it('说清在上还是在下', () => {
    expect(offsetText(shelf(-885, 50))).toBe('上方 50 点')
    expect(offsetText(shelf(-1355, -420))).toBe('下方 420 点')
  })
})

describe('edgeOf', () => {
  // LH2611−LH2705 @2026-08-19 的真实阶梯，现价差 −935。
  const ladder = [
    shelf(-885, 50),
    shelf(-1117, -182),
    shelf(-1210, -275),
    shelf(-1355, -420)
  ]

  it('做空取下方最近的做目标、上方最近的做止损', () => {
    const e = edgeOf(row(ladder), true)!
    expect(e.target.level).toBe('-1117')
    expect(e.stop.level).toBe('-885')
    expect(e.gain).toBe(182)
    expect(e.risk).toBe(50)
    expect(e.ratio).toBeCloseTo(3.64, 2)
  })

  it('做多整个翻过来 —— 同一个位置两个方向的盈亏比可以差很远', () => {
    const e = edgeOf(row(ladder), false)!
    expect(e.target.level).toBe('-885')
    expect(e.stop.level).toBe('-1117')
    expect(e.gain).toBe(50)
    expect(e.risk).toBe(182)
    // 0.27 vs 上面的 3.64 —— 这正是运营者说的「同一个位置两个方向风险不对称」。
    expect(e.ratio).toBeCloseTo(0.27, 2)
  })

  it('某一侧没有档位时给 null，不硬凑一个出来', () => {
    // 全在下方：做多没有目标。
    const onlyBelow = [shelf(-1117, -182), shelf(-1355, -420)]
    expect(edgeOf(row(onlyBelow), false)).toBeNull()
    expect(edgeOf(row(onlyBelow), true)).toBeNull() // 做空也没有止损
    expect(edgeOf(row([]), true)).toBeNull()
  })

  it('偏移为 0 的档位不算 —— 现价正踩在上面，它两边都不是', () => {
    const e = edgeOf(row([shelf(-935, 0), shelf(-885, 50), shelf(-1117, -182)]), true)!
    expect(e.target.level).toBe('-1117')
    expect(e.stop.level).toBe('-885')
  })

  it('取的是**最近**的一档，不是列表里的第一个', () => {
    // 故意把远的排在前面，防止实现依赖数组顺序。
    const jumbled = [shelf(-1355, -420), shelf(-1117, -182), shelf(-825, 110), shelf(-885, 50)]
    const e = edgeOf(row(jumbled), true)!
    expect(e.target.level).toBe('-1117')
    expect(e.stop.level).toBe('-885')
  })
})
