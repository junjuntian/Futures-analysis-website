import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NetPositionView from './NetPositionView.vue'
import { useAuthStore } from '../stores/auth'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn() })
}))

function response(data: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => ({ data, meta: { request_id: 'request-1' } })
  } as Response
}

/** 8-4 那天国泰掉出了前二十：他没有行，合计里少了他。 */
const DAYS = [
  {
    trade_date: '2026-08-03',
    open_price: '900',
    high_price: '910',
    low_price: '895',
    close_price: '905',
    net_position: '1200',
    long_lots: '1200',
    short_lots: '0',
    counted_members: ['中信期货', '国泰君安'],
    missing_members: []
  },
  {
    trade_date: '2026-08-04',
    open_price: '905',
    high_price: '915',
    low_price: '900',
    close_price: '912',
    net_position: '800',
    long_lots: '800',
    short_lots: '0',
    counted_members: ['中信期货'],
    missing_members: ['国泰君安']
  }
]

const FAVORITES = [{ id: 'fav-1', name: '五大机构', members: ['中信期货', '永安期货'] }]

let netPositionCalls: string[] = []

function stubFetch() {
  netPositionCalls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.includes('/seats/member-favorites')) return response(FAVORITES)
      if (url.includes('/seats/net-position')) {
        netPositionCalls.push(url)
        return response({
          instrument: 'AU',
          contract: null,
          is_variety_total: true,
          members: ['中信期货', '国泰君安'],
          all_members: ['中信期货', '国泰君安', '永安期货'],
          contracts: ['AU2612'],
          price_series_kind: 'open_interest_weighted',
          days: DAYS
        })
      }
      if (url.includes('/varieties')) {
        return response({ items: [{ symbol: 'AU', name: '沪金' }] })
      }
      return response({})
    })
  )
}

function mountPage() {
  return mount(NetPositionView, {
    global: { plugins: [ElementPlus], stubs: { SpreadChart: true } }
  })
}

describe('NetPositionView', () => {
  beforeEach(() => {
    localStorage.clear()
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    stubFetch()
  })

  it('把掉榜的那天说出来，而不是让它看着像减仓', async () => {
    localStorage.setItem('netPosition.instrument', 'AU')
    localStorage.setItem('netPosition.members', '中信期货,国泰君安')
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    // 合计从 1200 掉到 800 是因为少算了一家，不是他减了仓——页面必须点名。
    expect(text).toContain('国泰君安')
    expect(text).toContain('掉榜')
    wrapper.unmount()
  })

  it('所选席位以逗号拼进查询串，且不重复', async () => {
    localStorage.setItem('netPosition.instrument', 'AU')
    // 同一家写两遍：重复会被逐日加两遍，合计直接翻倍。
    localStorage.setItem('netPosition.members', '中信期货,中信期货')
    const wrapper = mountPage()
    await flushPromises()

    expect(netPositionCalls.length).toBeGreaterThan(0)
    const query = decodeURIComponent(netPositionCalls[netPositionCalls.length - 1])
    expect(query).toContain('members=中信期货')
    expect(query).not.toContain('中信期货,中信期货')
    wrapper.unmount()
  })

  it('收藏读出来后列在页面上，带成员家数', async () => {
    localStorage.setItem('netPosition.instrument', 'AU')
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('五大机构')
    wrapper.unmount()
  })
})
