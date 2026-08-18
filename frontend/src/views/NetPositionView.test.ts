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

/**
 * 三天各演一种数据状态：
 *
 * - 8-3 全员在榜，是干净的基准日；
 * - 8-4 国泰掉出前二十且推不出来，他那天没有行，合计里少了他；
 * - 8-5 国泰又没上榜，但这次由回榜日的增减反推出来了，计进了合计——
 *   这天的数不是实测的，界面得说清楚，不能和 8-3 长成一个样。
 *
 * mock 按 `NetPositionDay` 补齐字段。缺字段不是「测试写简单点」，是让组件在
 * 渲染期读到 undefined：`inferred_members.length` 就这么炸过一轮 unhandled error，
 * 74 个断言全绿而进程退 1。
 */
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
    missing_members: [],
    inferred_members: [],
    daily_pnl: null,
    cumulative_pnl: '0',
    long_cost: '898.50',
    long_cost_lots: '1200',
    short_cost: null,
    short_cost_lots: '0'
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
    missing_members: ['国泰君安'],
    inferred_members: [],
    daily_pnl: null,
    cumulative_pnl: '0',
    long_cost: '901.20',
    long_cost_lots: '800',
    short_cost: null,
    short_cost_lots: '0'
  },
  {
    trade_date: '2026-08-05',
    open_price: '912',
    high_price: '920',
    low_price: '908',
    close_price: '918',
    net_position: '1150',
    long_lots: '1200',
    short_lots: '50',
    counted_members: ['中信期货', '国泰君安'],
    missing_members: [],
    inferred_members: ['国泰君安'],
    daily_pnl: '480000',
    cumulative_pnl: '480000',
    long_cost: '903.40',
    long_cost_lots: '1200',
    short_cost: '917.10',
    short_cost_lots: '50'
  }
]

const FAVORITES = [{ id: 'fav-1', name: '五大机构', members: ['中信期货', '永安期货'] }]

let netPositionCalls: string[] = []
let postedCsrf: string | null = null

function stubFetch() {
  netPositionCalls = []
  postedCsrf = null
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.includes('/auth/csrf')) return response({ csrf_token: 'fresh-token' })
      if (url.includes('/seats/member-favorites')) {
        const headers = (init?.headers ?? {}) as Record<string, string>
        if (init?.method === 'POST') {
          postedCsrf = headers['x-csrf-token'] ?? null
          return response({ id: 'fav-2', name: '机构席位', members: ['中信期货'] })
        }
        return response(FAVORITES)
      }
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

  it('常驻摘要带多空手数与两腿成本，反推日单独点名', async () => {
    localStorage.setItem('netPosition.instrument', 'AU')
    localStorage.setItem('netPosition.members', '中信期货,国泰君安')
    const wrapper = mountPage()
    await flushPromises()

    const text = wrapper.text()
    // 只给一个「净 1,150 手」是不够的：它可能是「多 1,200 空 50」，也可能是
    // 「多 5,000 空 3,850」，两者的持仓结构与成本完全不是一回事。
    expect(text).toContain('多 1,200 手')
    expect(text).toContain('空 50 手')
    expect(text).toContain('净多成本 903.40')
    expect(text).toContain('净空成本 917.10')
    // 最后一天那个数是倒推来的，摘要里必须写明，不能和实测日长成一个样。
    expect(text).toContain('按回榜反推计入')
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

  it('保存收藏时先取写入保护令牌，不会送出空令牌', async () => {
    // store 里的令牌是懒加载的，刷新之后就是 null。直接 `csrfToken ?? ''` 送出去
    // 会被后端以 403「request is not allowed」拒掉——上线当天就是这么坏的，
    // 而当时的测试预设了令牌，所以一路绿灯。这里刻意不预设。
    expect(useAuthStore().csrfToken).toBeNull()
    localStorage.setItem('netPosition.instrument', 'AU')
    localStorage.setItem('netPosition.members', '中信期货')
    const wrapper = mountPage()
    await flushPromises()

    const inputs = wrapper.findAllComponents({ name: 'ElInput' })
    inputs[0].vm.$emit('update:modelValue', '机构席位')
    await flushPromises()
    const buttons = wrapper.findAllComponents({ name: 'ElButton' })
    const save = buttons.find((button) => button.text().includes('收藏当前'))
    expect(save).toBeTruthy()
    await save!.trigger('click')
    await flushPromises()

    expect(postedCsrf).toBe('fresh-token')
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
