import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SeatsView from './SeatsView.vue'
import { useAuthStore } from '../stores/auth'

// 页面用的是 composition API 的 useRoute/useRouter，global.mocks 里的 $route 够不着它。
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'building' } }),
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
 * 净持仓子页的数据。所选席位在 AU 上历史持有过 AU2612，**没有** AU2608——后者已到期。
 *
 * 走的是 `/seats/net-position`：净持仓子页（原「建仓过程」那四张图）从 2026-08-18 起
 * 读合计接口，一家是多家的特例。
 */
const BUILDING = {
  instrument: 'AU',
  contract: null,
  is_variety_total: true,
  price_multiplier: '1000',
  members: ['中信'],
  all_members: ['中信', '国泰君安'],
  contracts: ['AU2612'],
  price_series_kind: 'open_interest_weighted',
  days: []
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.includes('/seats/member-instruments')) {
        return response({ member: '中信', instruments: ['AU'] })
      }
      if (url.includes('/seats/net-position')) return response(BUILDING)
      if (url.includes('/seats/favorites')) return response([])
      if (url.includes('/seats/positions')) {
        return response({
          member: '中信',
          instrument: null,
          members: ['中信', '国泰君安'],
          trade_date: '2026-08-14',
          available_dates: ['2026-08-14'],
          coverage_start: '2010-01-04',
          rows: []
        })
      }
      return response({})
    })
  )
}

function mountPage() {
  return mount(SeatsView, {
    global: {
      plugins: [ElementPlus],
      stubs: { SpreadChart: true, RouterLink: true }
    }
  })
}

describe('SeatsView 的选择记忆', () => {
  beforeEach(() => {
    localStorage.clear()
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    stubFetch()
  })

  it('把上次选的品种与合约从 localStorage 读回来', async () => {
    localStorage.setItem('seats.instrument', 'AU')
    localStorage.setItem('seats.contract', 'AU2612')
    const wrapper = mountPage()
    await flushPromises()
    // 合约仍在接口给的 contracts 里，应当原样保留。
    expect(localStorage.getItem('seats.instrument')).toBe('AU')
    expect(localStorage.getItem('seats.contract')).toBe('AU2612')
    wrapper.unmount()
  })

  it('记住的合约已到期时退回合约汇总，而不是停在一张空表上', async () => {
    localStorage.setItem('seats.instrument', 'AU')
    // AU2608 不在 contracts 里——期货合约会到期，这是常态不是异常。
    localStorage.setItem('seats.contract', 'AU2608')
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()
    expect(localStorage.getItem('seats.contract')).toBe('')
    wrapper.unmount()
  })

  it('「合约汇总」这个选择本身也要记住', async () => {
    // 空值不写进去的话，他主动切回汇总，下次进来又会被翻出上一个合约。
    localStorage.setItem('seats.instrument', 'AU')
    localStorage.setItem('seats.contract', 'AU2612')
    const wrapper = mountPage()
    await flushPromises()

    const selects = wrapper.findAllComponents({ name: 'ElSelect' })
    const contractSelect = selects.find((select) => select.props('modelValue') === 'AU2612')
    expect(contractSelect).toBeTruthy()
    contractSelect!.vm.$emit('update:modelValue', '')
    await flushPromises()

    expect(localStorage.getItem('seats.contract')).toBe('')
    wrapper.unmount()
  })

  it('单选时代存的 seats.member 仍然读得回来', async () => {
    // 运营者本地存着旧键。直接改读新键会让他打开页面时发现选择被清空了——
    // 这种「升级即丢设置」的事一次都不该发生。
    localStorage.setItem('seats.member', '中信')
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('中信')
    // 读回来之后按新键写，旧键从此不再被写入。
    expect(localStorage.getItem('seats.members')).toBe('中信')
    wrapper.unmount()
  })

  it('所选席位逐家取一次持仓，不合并成一张表', async () => {
    localStorage.setItem('seats.members', '中信,国泰君安')
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    // 榜单的「名次」与「增减量」是逐家公布的，加起来没有意义，所以是各取各的。
    const calls = (fetch as unknown as { mock: { calls: Array<[RequestInfo | URL]> } }).mock.calls
      .map(([input]) => input.toString())
      .filter((url) => url.includes('/seats/positions') && url.includes('member='))
    const asked = calls.map((url) => decodeURIComponent(url))
    expect(asked.some((url) => url.includes('member=中信'))).toBe(true)
    expect(asked.some((url) => url.includes('member=国泰君安'))).toBe(true)
    wrapper.unmount()
  })
})
