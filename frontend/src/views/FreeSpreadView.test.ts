import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FreeSpreadView from './FreeSpreadView.vue'
import { useAuthStore } from '../stores/auth'

const source = {
  provider: 'sanhe',
  source_code: 'sanhe_spread_readonly',
  source_display_name: '三禾数据',
  source_type: 'aggregator',
  fetched_at: '2026-08-05T08:00:00Z',
  data_cutoff_at: '2026-08-04',
  price_basis: 'upstream_spread',
  raw_leg_prices_available: false
}

function response(data: unknown) {
  return { ok: true, status: 200, json: async () => ({ data, meta: { request_id: 'request-1' } }) } as Response
}

/**
 * Drives the four Element Plus selects the way a user would. The page no
 * longer preselects a combination, so a query test has to pick the legs first.
 */
async function selectBothLegs(wrapper: ReturnType<typeof mount>) {
  const selects = wrapper.findAllComponents({ name: 'ElSelect' })
  for (const index of [0, 2]) {
    selects[index].vm.$emit('update:modelValue', '焦煤')
    selects[index].vm.$emit('change', '焦煤')
    await flushPromises()
  }
}

describe('FreeSpreadView', () => {
  beforeEach(() => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/varieties')) {
        return response({
          source,
          result_kind: 'ok',
          items: [{ market: '大商所', name: '焦煤', symbol: 'JM' }]
        })
      }
      if (url.includes('/varieties/') && url.endsWith('/months')) {
        return response({ source, variety: '焦煤', months: ['01', '05', '09'], basis: 1,
          basis_semantics_confirmed: false, result_kind: 'ok' })
      }
      if (url.endsWith('/favorites')) return response([])
      if (url.endsWith('/free-spread/query')) {
        const trace = {
          provider: 'sanhe', source_code: source.source_code, data_cutoff_at: '2026-08-04',
          price_basis: 'upstream_spread', sample_start: '2026-01-02', sample_end: '2026-01-05',
          sample_count: 2, excluded_point_count: 1, calendar_version_ids: ['calendar-1'],
          window_algorithm_version: 'retail_window_v1', statistics_algorithm_version: 'spread_window_stats_v1',
          rule_version: 'retail-window-default-v1'
        }
        return response({
          series_id: 'series-1', source,
          query: { provider: 'sanhe', leg1: { variety: '焦煤', symbol: 'JM', month: '09' },
            leg2: { variety: '焦煤', symbol: 'JM', month: '01' } },
          quality: { status: 'ok', input_point_count: 3, retained_point_count: 2,
            excluded_point_count: 1, missing_contract_point_count: 0 },
          algorithm_versions: { provider: 'sanhe_spread_v1', window: 'retail_window_v1',
            statistics: 'spread_window_stats_v1', rule: 'retail-window-default-v1' },
          continuous_series: { trace, points: [
            { trade_date: '2026-01-02', value: -2, from_code: 'jm2609', to_code: 'jm2701', segment_no: 1 },
            { trade_date: '2026-01-05', value: 3, from_code: 'jm2609', to_code: 'jm2701', segment_no: 1 }
          ], segment_boundaries: [], current_value: 3 },
          seasonal_series: { trace, axis: ['01-02', '01-05'], years: [], current_year: 2026 },
          monthly_matrix: { trace, years: [], up_ratios: Array.from({ length: 12 }, (_, index) => ({
            month: index + 1, ratio: null, positive_year_count: 0, eligible_year_count: 0
          })) },
          segments: []
        })
      }
      throw new Error(`unexpected URL ${url}`)
    }))
  })

  it('uses only platform APIs and discloses the real source', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    const wrapper = mount(FreeSpreadView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: { SpreadChart: { template: '<div class="chart-stub" />' } }
      }
    })
    await flushPromises()
    await selectBothLegs(wrapper)
    const viewButton = wrapper.findAll('button').find((button) => button.text() === '查看')
    expect(viewButton).toBeDefined()
    await viewButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('数据来源：三禾数据')
    expect(wrapper.text()).toContain('仅统计散户可交易窗口 · 当前 3')
    const urls = vi.mocked(fetch).mock.calls.map(([input]) => input.toString())
    expect(urls.every((url) => url.startsWith('/api/v1/'))).toBe(true)
    expect(urls.some((url) => url.includes('sanheshuju.com'))).toBe(false)
  })

  it('opens with both legs empty and queries nothing', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(FreeSpreadView, {
      global: { plugins: [pinia, ElementPlus], stubs: { SpreadChart: true } }
    })
    await flushPromises()

    const selects = wrapper.findAllComponents({ name: 'ElSelect' })
    expect(selects).toHaveLength(4)
    expect(selects.every((select) => !select.props('modelValue'))).toBe(true)
    const viewButton = wrapper.findAll('button').find((button) => button.text() === '查看')
    expect(viewButton!.attributes('disabled')).toBeDefined()
    const urls = vi.mocked(fetch).mock.calls.map(([input]) => input.toString())
    expect(urls.some((url) => url.endsWith('/free-spread/query'))).toBe(false)
    expect(urls.some((url) => url.endsWith('/months'))).toBe(false)
  })

  /**
   * 2026-09-02:HTTP/2 连接闲置 65 秒后被 nginx 关掉,浏览器复用它发 POST 就
   * 直接抛 `Failed to fetch` —— 服务器日志里一行都没有,因为请求没送到。
   * GET 浏览器会自己重发,POST 不会,所以这个重试必须我们自己做。
   */
  it('连接失效导致的失败会自动重发一次,用户看不到报错', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    const realFetch = vi.mocked(fetch).getMockImplementation()!
    let attempts = 0
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith('/free-spread/query') && attempts++ === 0) {
        throw new TypeError('Failed to fetch')
      }
      return realFetch(input, init)
    })
    const wrapper = mount(FreeSpreadView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: { SpreadChart: { template: '<div class="chart-stub" />' } }
      }
    })
    await flushPromises()
    await selectBothLegs(wrapper)
    await wrapper.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    await flushPromises()

    expect(attempts).toBe(2)
    expect(wrapper.text()).toContain('仅统计散户可交易窗口 · 当前 3')
    expect(wrapper.text()).not.toContain('Failed to fetch')
  })

  it('重发也失败时给中文说明,不把 `Failed to fetch` 原样甩给运营者', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    const realFetch = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith('/free-spread/query')) throw new TypeError('Failed to fetch')
      return realFetch(input, init)
    })
    const wrapper = mount(FreeSpreadView, {
      global: { plugins: [pinia, ElementPlus], stubs: { SpreadChart: true } }
    })
    await flushPromises()
    await selectBothLegs(wrapper)
    await wrapper.findAll('button').find((button) => button.text() === '查看')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('没送到服务器')
    expect(wrapper.text()).toContain('重试当前组合')
    expect(wrapper.text()).not.toContain('Failed to fetch')
  })

  it('ships no built-in preset combinations', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(FreeSpreadView, {
      global: { plugins: [pinia, ElementPlus], stubs: { SpreadChart: true } }
    })
    await flushPromises()

    const labels = wrapper.findAll('button').map((button) => button.text())
    for (const preset of ['卷螺差', '豆棕差', '油粕比', '玻璃-纯碱', '焦煤 9-1']) {
      expect(labels).not.toContain(preset)
    }
    // The favourite row keeps its heading hidden until something is saved.
    expect(wrapper.text()).not.toContain('常用')
    expect(wrapper.text()).not.toContain('收藏组合')
  })
})
