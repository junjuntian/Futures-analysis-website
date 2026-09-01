import { describe, expect, it } from 'vitest'
import { dayCompleteness, rankByExtremity, type MonitorItemLike } from './overview'

function item(name: string, pair: number | null, years: number | null, alert = 'high'): MonitorItemLike & { name: string } {
  return {
    name,
    alert,
    pair: { position: pair === null ? null : String(pair), alert: null },
    years: years === null ? null : { position: String(years), alert: 'high' }
  }
}

describe('触发组合排序', () => {
  it('低位和高位一样靠前——比的是离中线多远,不是位置本身', () => {
    // 直接按位置排序会把 0.02 这种极低位排到最后,而它和 0.98 一样值得看。
    const ranked = rankByExtremity([
      item('中间', 0.55, null),
      item('极低', 0.02, null),
      item('极高', 0.98, null)
    ])
    expect(ranked.map((r) => (r as { name: string }).name)).toEqual(['极低', '极高', '中间'])
  })

  it('一个组合取当年与历年里更极端的那条轨', () => {
    // 当年温和、历年极端的组合不该被当年那条数字埋掉。
    const ranked = rankByExtremity([
      item('历年极端', 0.5, 0.99),
      item('当年略高', 0.7, 0.55)
    ])
    expect((ranked[0] as { name: string }).name).toBe('历年极端')
  })

  it('没触发的组合不进榜', () => {
    const ranked = rankByExtremity([item('未触发', 0.99, null, null as unknown as string)])
    expect(ranked).toEqual([])
  })

  it('位置缺失不会把它误排到最前', () => {
    // 位置为空是「算不出来」,extremity 记 0 排最后;若当成极值会顶掉真正的极端组合。
    const ranked = rankByExtremity([item('无位置', null, null), item('极高', 0.97, null)])
    expect((ranked[0] as { name: string }).name).toBe('极高')
  })

  it('最多取 limit 条', () => {
    const many = Array.from({ length: 8 }, (_, i) => item(`c${i}`, 0.95, null))
    expect(rankByExtremity(many)).toHaveLength(3)
    expect(rankByExtremity(many, 5)).toHaveLength(5)
  })
})

describe('数据到齐判定', () => {
  const seats = [
    { trade_date: '2026-08-12', exchanges: ['CZCE', 'DCE', 'SHFE'] },
    { trade_date: '2026-08-11', exchanges: ['CZCE', 'SHFE'] }
  ]
  const prices = [
    { trade_date: '2026-08-12', exchanges: ['CZCE', 'DCE', 'SHFE'] },
    { trade_date: '2026-08-11', exchanges: ['CZCE', 'DCE', 'SHFE'] }
  ]

  it('三所齐才算到齐', () => {
    const result = dayCompleteness(seats, prices, 3, 3)
    expect(result).toEqual([
      { trade_date: '2026-08-12', complete: true },
      // 08-11 席位少了大商所:行情齐也不算到齐。
      { trade_date: '2026-08-11', complete: false }
    ])
  })

  it('行情整天缺失也算不齐', () => {
    // 席位到了、行情没到,是真实发生过的情形(新浪那条链断掉时)。
    expect(dayCompleteness(seats, [], 3, 3)[0].complete).toBe(false)
  })

  it('一家交易所都没见过时不算全绿', () => {
    // 空库的 expectedCount 是 0。少了这道判断,新装的站会显示一片绿,
    // 而实际上一行数据都没有。
    expect(dayCompleteness([{ trade_date: '2026-08-12', exchanges: [] }], [], 0, 0)).toEqual([
      { trade_date: '2026-08-12', complete: false }
    ])
  })

  it('行情多一家交易所(INE 只有行情没有席位)不算缺口', () => {
    // **2026-09-01 实际发生的 bug**:期望值原来只有一个,取的是席位与行情的并集。
    // INE(上期能源)按设计只有行情没有席位(DEC-158:能源中心不披露原油席位排名),
    // 于是并集是 5、席位永远只有 4,判定恒为 false ——「有缺口」从三品种上线那天起
    // 一直亮着。**一个永远为真的告警等于没有告警**,运营者问「这个有缺口是什么意思」
    // 才发现。这条钉住:两边期望分开传,各自齐了就算齐。
    const seatDays = [{ trade_date: '2026-09-01', exchanges: ['CFFEX', 'CZCE', 'DCE', 'SHFE'] }]
    const priceDays = [
      { trade_date: '2026-09-01', exchanges: ['CFFEX', 'CZCE', 'DCE', 'INE', 'SHFE'] }
    ]
    expect(dayCompleteness(seatDays, priceDays, 4, 5)[0].complete).toBe(true)
    // 反面:真少一家席位时照样报缺口,别把门槛放松成永远为真
    const missing = [{ trade_date: '2026-09-01', exchanges: ['CFFEX', 'CZCE', 'SHFE'] }]
    expect(dayCompleteness(missing, priceDays, 4, 5)[0].complete).toBe(false)
  })

  it('只取最近 limit 天', () => {
    const many = Array.from({ length: 20 }, (_, i) => ({
      trade_date: `2026-08-${String(20 - i).padStart(2, '0')}`,
      exchanges: ['A']
    }))
    expect(dayCompleteness(many, many, 1, 1)).toHaveLength(10)
  })
})
