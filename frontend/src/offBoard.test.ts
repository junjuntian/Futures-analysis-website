import { describe, expect, it } from 'vitest'
import { offBoardBands } from './offBoard'

/** 简写：'x' 表示那天不在榜上。 */
function series(pattern: string) {
  return pattern.split('').map((flag, index) => ({
    trade_date: `d${index + 1}`,
    known: flag !== 'x'
  }))
}

describe('掉榜区间', () => {
  it('全程在榜就没有区间', () => {
    expect(offBoardBands(series('.....'))).toEqual([])
  })

  it('掉一天：区间只覆盖那一天，不蔓延到回榜日', () => {
    // 生产实例：高盛 AU2610 多头 07-28 在榜、07-29 掉榜、07-30 回榜。
    // 区间画到 07-30 就等于说他回榜那天也没数据，把有据可查的一天涂成了空白。
    expect(offBoardBands(series('.x.'))).toEqual([[{ xAxis: 'd2' }, { xAxis: 'd2' }]])
  })

  it('连着掉几天并成一段', () => {
    expect(offBoardBands(series('.xxx.'))).toEqual([[{ xAxis: 'd2' }, { xAxis: 'd4' }]])
  })

  it('中间回榜一天就断成两段', () => {
    // 并成一段等于把他回榜的那天也说成未知。
    expect(offBoardBands(series('.xx.xx.'))).toEqual([
      [{ xAxis: 'd2' }, { xAxis: 'd3' }],
      [{ xAxis: 'd5' }, { xAxis: 'd6' }]
    ])
  })

  it('序列以掉榜收尾时区间开到最后一天', () => {
    // 最常见的一种：该合约临近交割，席位早就掉出前 20 了。
    expect(offBoardBands(series('..xx'))).toEqual([[{ xAxis: 'd3' }, { xAxis: 'd4' }]])
  })

  it('开头就掉榜也算一段', () => {
    expect(offBoardBands(series('xx...'))).toEqual([[{ xAxis: 'd1' }, { xAxis: 'd2' }]])
  })

  it('整段都不在榜', () => {
    expect(offBoardBands(series('xxx'))).toEqual([[{ xAxis: 'd1' }, { xAxis: 'd3' }]])
  })

  it('空序列不出区间', () => {
    expect(offBoardBands([])).toEqual([])
  })
})
