import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import HogMoney from './HogMoney.vue'

/**
 * 夹具照抄引擎真实产出的形状(engine/hog_money.py 跑出来的 hog_signals.json),
 * 字段一个不缺——mock 缺字段会让组件在渲染期读到 undefined,而断言照样全绿、
 * 只有退出码是 1(2026-08-18 踩过)。
 *
 * 这一版刻意选**当前正持有做多、且已跨过主力换月**的状态:它同时盖住两个最容易
 * 出错的展示点——做多要标「未验证」、跨换月的进场价与现价不在同一合约。
 */
const PAYLOAD = {
  instrument: 'LH',
  name: '生猪 LH',
  unit: '元/吨',
  multiplier: 16,
  data_date: '2026-08-18',
  computed_at: '2026-08-19 05:30:00',
  state: '做多中',
  contract: 'LH2611',
  price: 12485,
  signal: { z: 0.22, enter: 1, net: -23952, change: 1482, win: 5, suggested_position: 0.22 },
  position: {
    side: 'long', entry_date: '2026-08-04', exit_date: null, entry_px: 10825,
    exit_px: null, contract: 'LH2609', ret_pct: 5.23, hold_days: 9, exit_reason: null
  },
  institution: {
    net: -16953, side: 'net_short' as const, just_flipped_long: false,
    long_enabled: false, long_signal_now: false
  },
  members: [
    { member: '国泰君安', net: -11413, change: 3472, on_board: true },
    { member: '东证期货', net: -2533, change: -120, on_board: true },
    { member: '东吴期货', net: -1200, change: null, on_board: false }
  ],
  group_log: [
    { date: '2026-08-01', members: ['国泰君安', '东证期货'], alpha: { 国泰君安: 5.62, 东证期货: 5.38 } }
  ],
  history: [
    { side: 'short', entry_date: '2026-06-02', exit_date: '2026-06-20', entry_px: 13000,
      exit_px: 12500, contract: 'LH2609', ret_pct: 3.85, hold_days: 14, exit_reason: '反向' },
    { side: 'long', entry_date: '2026-04-02', exit_date: '2026-04-13', entry_px: 12000,
      exit_px: 11200, contract: 'LH2605', ret_pct: -6.31, hold_days: 6, exit_reason: '止损' }
  ],
  stats: {
    trades: 2, win_rate: 50, avg_pct: -1.23, cum_pct: -2.7,
    short_trades: 1, long_trades: 1
  },
  rules: { enter: 1, stop: 0.06, reselect_months: 12 },
  compare: {
    strategy: { cum_pct: 86.5, sharpe: 1.96, max_dd_pct: -8.6 },
    benchmark: { cum_pct: 99.2, sharpe: 1.65, max_dd_pct: -14.8 },
    benchmark_name: '恒定满仓做空',
    note: '同一段区间、同一口径。'
  },
  caveats: ['样本只有三年,且只有一种市况——全程熊市。', '做多信号未经验证。']
}

function stubFetch(payload: unknown = PAYLOAD, ok = true) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok, status: ok ? 200 : 404, json: async () => payload
  } as Response)))
}

describe('生猪机构资金', () => {
  beforeEach(() => stubFetch())

  it('读引擎产出的 JSON 并渲染当前状态', async () => {
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('生猪 LH')
    expect(t).toContain('做多中')
    expect(t).toContain('+5.23%')
    // 机构合计流向是这套策略的主信号,必须出现在首屏
    expect(t).toContain('机构合计净持仓')
    w.unmount()
  })

  it('做多持仓必须标注未验证——不能和空头信号看起来一样可信', async () => {
    // 回测里多头 15 笔累计仅 +4.5%,且样本期内机构一天都没转成净多。
    // 这条提示是运营者验收的硬要求,不是装饰。
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('未验证')
    expect(w.find('.caveat-box').exists()).toBe(true)
    expect(w.find('.strip-pill').classes()).toContain('unverified')
    w.unmount()
  })

  it('跨过主力换月时,状态条的合约跟着现价走,并说明两个价格不可相减', async () => {
    // 进场在 LH2609 @10825,现价是 LH2611 的 12485,看着像涨 15%,实际 +5.23%。
    // 生猪各合约价差最大 49%,并排摆着不说明,就是在诱导人做减法。
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.strip-name').text()).toContain('LH2611')
    const t = w.text()
    expect(t).toContain('不要相减')
    expect(t).toContain('LH2609')
    w.unmount()
  })

  it('空仓时不渲染状态条,也不出未验证提示', async () => {
    stubFetch({ ...PAYLOAD, state: '观察中', position: null })
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.symbol-strip').exists()).toBe(false)
    expect(w.find('.caveat-box').exists()).toBe(false)
    expect(w.text()).toContain('无持仓')
    w.unmount()
  })

  it('历史页把多空分开统计,做多那栏挂未验证徽标', async () => {
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[1].trigger('click')
    const t = w.text()
    expect(t).toContain('做空 1 笔')
    expect(t).toContain('做多 1 笔')
    expect(w.find('.badge.warn').text()).toContain('未验证')
    expect(w.find('.badge.ok').text()).toContain('有回测支撑')
    w.unmount()
  })

  it('历史页必须摆出与「躺着做空」的对比', async () => {
    // 三年单边熊市里,什么都不做地持有空单本身就有 +99% 复利。不给基准,
    // 上面那个累计收益会被当成策略的本事(运营者 2026-08-19 正是问到这一点)。
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[1].trigger('click')
    const t = w.find('.compare').text()
    expect(t).toContain('恒定满仓做空')
    expect(t).toContain('+99.20%')   // 基准累计
    expect(t).toContain('+86.50%')   // 策略累计——比基准低,也要如实显示
    w.unmount()
  })

  it('翻页用 el-pagination,与金银历史信号一致', async () => {
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[1].trigger('click')
    expect(w.findComponent({ name: 'ElPagination' }).exists()).toBe(true)
    w.unmount()
  })

  it('机构真转多时要醒目提示,但明说不会进场', async () => {
    // 运营者盯的就是这个拐点。做多支路关着不代表这件事不该报——但也不能让他
    // 以为系统会跟:样本里转多之后 20 日主力仍平均跌 1.18%。
    stubFetch({
      ...PAYLOAD, position: null, state: '观察中',
      institution: { ...PAYLOAD.institution, net: 4046, side: 'net_long', just_flipped_long: true }
    })
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const box = w.find('.caveat-box.flip')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('不会因此进场')
    expect(w.text()).toContain('净多')
    w.unmount()
  })

  it('机构只是减空(未转多)时,说明为什么不进场', async () => {
    stubFetch({
      ...PAYLOAD, position: null, state: '观察中',
      signal: { ...PAYLOAD.signal, z: 1.4 },
      institution: { ...PAYLOAD.institution, long_signal_now: true }
    })
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.find('.caveat-box').text()
    expect(t).toContain('做多支路是关闭的')
    // 「减空」不等于「转多」——机构此刻仍是净空,这句必须在
    expect(t).toContain('净空')
    w.unmount()
  })

  it('取不到 JSON 时给出错误,而不是白屏', async () => {
    stubFetch(null, false)
    const w = mount(HogMoney, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.err').exists()).toBe(true)
    w.unmount()
  })
})
