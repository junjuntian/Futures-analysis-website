import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SmartMoneyView from './SmartMoneyView.vue'

/** 造 count 条历史信号，日期从新到旧。 */
function history(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    market: 'AU',
    name: '黄金',
    signal_date: `2026-01-${String((index % 28) + 1).padStart(2, '0')}`,
    seats: [{ member: '中信期货', strength: 1.1 }],
    score: 6.1,
    zone: null,
    inst_cost: null,
    entry_date: '2026-01-05',
    entry_px: 900,
    exit_date: null,
    exit_px: null,
    result: '持有中',
    relay: false,
    ret_pct: null,
    marks: { cross_resonance: false, spread_legs: [], goldman_combo: false }
  }))
}

/** 组件挂载时先渲染「今日信号」，AU 和 AG 两个市场都得在，缺一个就崩在模板里。 */
function market(instrument: string, name: string) {
  const condition = { value: 1, target: 2, pass: false }
  return {
    instrument,
    name,
    state: '空仓',
    last_close: 900,
    main_contract: `${instrument}2612`,
    conditions: { score: condition, dist_low: condition, netq: condition },
    all_pass: false,
    prospective_zone: null,
    prospective_cost: null,
    position: null,
    weights: {},
    theta: 5
  }
}

const PAYLOAD = {
  generated_at: '2026-08-15 00:35:23',
  data_date: '2026-08-14',
  markets: { AU: market('AU', '黄金'), AG: market('AG', '白银') },
  ratio: { value: 80, zone: '中', note: '', percentile: 50, mean: 78 },
  alerts: [],
  activity: [],
  history: history(135),
  alert_history: [],
  stats: { AU: { count: 55, win_rate: 61.8, avg: 2.27, total: 124.6, since: '2019' } },
  rules: { group: [], buy: '', sell: '', cond_seats: [] }
}

function bodyRows(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('tbody tr').length
}

describe('SmartMoneyView 历史信号分页', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => PAYLOAD }) as Response)
    )
  })

  async function openHistory() {
    const wrapper = mount(SmartMoneyView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const tabs = wrapper.findAll('.tab')
    await tabs[1].trigger('click') // 历史信号
    await flushPromises()
    return wrapper
  }

  it('品种按钮由 FLOW 列表渲染,加品种只改那一行', async () => {
    // 2026-08-19 加鸡蛋/焦煤时把四个写死的按钮改成 v-for。这条钉住的是
    // 「按钮数量 = 合计流向品种数 + 金银」,以后再加品种不改模板也不会漏。
    //
    // **上证50 是有意单列的**(DEC-172,2026-09-01):它的信号是「跟某几家席位
    // 的在场方向」,与其余五个品种的阵营 z 分数不是一回事,渲染的组件也不同
    // (IhFollow 而非 HogMoney)。所以它**不在 FLOW 里**,是模板里单独一个按钮 ——
    // 把它塞进 FLOW 会让 v-for 渲染出一个点了会崩的标签。
    const wrapper = mount(SmartMoneyView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const labels = wrapper.findAll('.variety').map((b) => b.text())
    expect(labels).toEqual(['黄金白银', '生猪', '鸡蛋', '焦煤', '玻璃', '纯碱', '上证50'])
    wrapper.unmount()
  })

  it('默认每页 20 条，总数报的是全量而不是这一页', async () => {
    const wrapper = await openHistory()
    expect(bodyRows(wrapper)).toBe(20)
    // 表头写的是全量条数——写成 20 会让人以为历史只有这么多。
    expect(wrapper.text()).toContain('共 135 条')
    wrapper.unmount()
  })

  it('翻到第二页换一批，不是同一批', async () => {
    const wrapper = await openHistory()
    const pagination = wrapper.findComponent({ name: 'ElPagination' })
    pagination.vm.$emit('update:current-page', 2)
    await flushPromises()
    expect(bodyRows(wrapper)).toBe(20)
    wrapper.unmount()
  })

  it('改每页条数会回到第一页', async () => {
    // 停在第 7 页再把每页从 10 改成 50，第 7 页早已超出总页数，表会变成空的，
    // 看上去像数据没了。
    const wrapper = await openHistory()
    const pagination = wrapper.findComponent({ name: 'ElPagination' })
    pagination.vm.$emit('update:current-page', 7)
    await flushPromises()
    pagination.vm.$emit('size-change', 50)
    await flushPromises()
    expect(bodyRows(wrapper)).toBe(50)
    wrapper.unmount()
  })

  it('最后一页只剩零头，不会补足一页', async () => {
    const wrapper = await openHistory()
    const pagination = wrapper.findComponent({ name: 'ElPagination' })
    pagination.vm.$emit('size-change', 50)
    await flushPromises()
    pagination.vm.$emit('update:current-page', 3) // 135 = 50 + 50 + 35
    await flushPromises()
    expect(bodyRows(wrapper)).toBe(35)
    wrapper.unmount()
  })
})
