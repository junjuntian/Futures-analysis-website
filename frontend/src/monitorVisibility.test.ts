import { describe, expect, it } from 'vitest'

import { visibleItems } from './monitorVisibility'

const rows = [
  { name: '在窗内', expired: false },
  { name: '今天刚关窗', expired: true },
  { name: '早就关窗、行停在 08-27', expired: true }
]

describe('visibleItems', () => {
  it('当前视图不显示过期组合 —— 它下不了单,卡上却在报止损与目标价', () => {
    expect(visibleItems(rows, false).map((row) => row.name)).toEqual(['在窗内'])
  })

  it('历史模式全留 —— 回看历年进场日,那时的组合到今天当然都过期了', () => {
    expect(visibleItems(rows, true)).toHaveLength(3)
  })

  it('不改原数组(计算属性会反复调它)', () => {
    const input = [...rows]
    visibleItems(input, true)
    visibleItems(input, false)
    expect(input).toHaveLength(3)
  })
})
