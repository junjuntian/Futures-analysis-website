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
  retail: {
    members: [
      { member: '东方财富', net: 3591, change: -4386, on_board: true },
      { member: '平安期货', net: 1486, change: -269, on_board: true },
      { member: '徽商期货', net: 1862, change: -2319, on_board: true }
    ],
    net: 6939, change: -6974, z: 1.11, resonate: true, trades: false,
    note: '散户三家长期站多头、长期亏钱,故反向取用。当前只作展示,不参与进出场。'
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
    short_trades: 1, long_trades: 1, exit_reasons: { 反向: 1, 止损: 1 }
  },
  rules: { enter: 1, stop: 0.06, reselect_months: 12, group_k: 5, max_hold: 40,
           sig_win: 5, long_enabled: false, exit_before_delivery: 10 },
  delivery: { window_end: '2026-10-30', days_left: 53, limit: 10, must_exit: false },
  compare: {
    strategy: { cum_pct: 86.5, sharpe: 1.96, max_dd_pct: -8.6 },
    benchmark: { cum_pct: 99.2, sharpe: 1.65, max_dd_pct: -14.8 },
    benchmark_name: '恒定满仓做空',
    note: '同一段区间、同一口径。'
  },
  caveats: ['样本只有三年,且只有一种市况——全程熊市。', '做多信号未经验证。']
}

/** 成本那一列走净持仓接口(Rust 侧的成本引擎),与信号 JSON 是两条路。 */
const NET_POSITION = {
  data: {
    latest_members: [
      { member: '国泰君安', long_lots: '0', long_cost: null, long_cost_lots: '0',
        short_lots: '11413', short_cost: '12835.23', short_cost_lots: '11413',
        missing: false, inferred: false },
      { member: '东证期货', long_lots: '0', long_cost: null, long_cost_lots: '0',
        short_lots: '2533', short_cost: '13074.92', short_cost_lots: '1200',
        missing: false, inferred: false },
      { member: '东吴期货', long_lots: '0', long_cost: null, long_cost_lots: '0',
        short_lots: '0', short_cost: null, short_cost_lots: '0',
        missing: true, inferred: false }
    ]
  },
  meta: { request_id: 'r1' }
}

function stubFetch(payload: unknown = PAYLOAD, ok = true) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString().includes('/seats/net-position')) {
      return { ok: true, status: 200, json: async () => NET_POSITION } as Response
    }
    return { ok, status: ok ? 200 : 404, json: async () => payload } as Response
  }))
}

describe('生猪机构资金', () => {
  beforeEach(() => stubFetch())

  it('引擎指纹对不上就挂「这份信号是旧引擎算的」', async () => {
    // DEC-099:页面读的是每日任务产出的静态 JSON,部署只换代码不重算 JSON。
    // 2026-08-20 DEC-096 上线后就这样过了一夜,而且看不出来 —— 这条是兜底。
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.includes('engine.json')) {
        return { ok: true, status: 200, json: async () => ({ fingerprint: '新引擎指纹' }) } as Response
      }
      if (url.includes('/seats/net-position')) {
        return { ok: true, status: 200, json: async () => NET_POSITION } as Response
      }
      return {
        ok: true, status: 200,
        json: async () => ({ ...PAYLOAD, engine_fingerprint: '旧引擎指纹' })
      } as Response
    }))
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('这份信号是旧引擎算的')
  })

  it('指纹一致、或任一边缺失时都不报 —— 缺字段不是过期', async () => {
    const mk = (payloadFp: string | undefined, liveFp: unknown) =>
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
        const url = input.toString()
        if (url.includes('engine.json')) {
          return { ok: true, status: 200, json: async () => ({ fingerprint: liveFp }) } as Response
        }
        if (url.includes('/seats/net-position')) {
          return { ok: true, status: 200, json: async () => NET_POSITION } as Response
        }
        return {
          ok: true, status: 200,
          json: async () => ({ ...PAYLOAD, engine_fingerprint: payloadFp })
        } as Response
      }))
    for (const [a, b] of [['同一个', '同一个'], [undefined, '新的'], ['旧的', undefined]] as const) {
      mk(a, b)
      const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
      await flushPromises()
      expect(w.text()).not.toContain('这份信号是旧引擎算的')
      w.unmount()
    }
  })

  it('够不上风险门槛的品种不挂风险条', async () => {
    // 生猪现在 0 条(夏普 2.23、回撤 −6.8%、胜率 61.1%、t=2.52)。
    // 门槛写死在引擎里、数字实算——不是按品种硬编码,所以这里也不能按品种断言。
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).not.toContain('这条曲线不好拿住')
  })

  it('风险条摆在收益数字前面 —— 放在下面等于没放', async () => {
    stubFetch({
      ...PAYLOAD,
      risk_flags: [
        { key: 'sharpe', text: '**夏普只有 0.61** —— 一年赚到的抵不上一年的波动。' },
        { key: 'drawdown', text: '**最大回撤 -43.1%** —— 中途要扛得住净值腰斩级别的下跌。' }
      ]
    })
    const w = mount(HogMoney, { props: { instrument: 'FG' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('这条曲线不好拿住')
    expect(t).toContain('夏普只有 0.61')
    // 位置:必须出现在「累计」那块数字之前
    expect(t.indexOf('这条曲线不好拿住')).toBeLessThan(t.indexOf('累计'))
    // **加粗** 要变成真的 <b>,不能把星号原样印出来
    expect(w.html()).toContain('<b>夏普只有 0.61</b>')
    expect(w.find('.risk-banner').text()).not.toContain('**')
  })

  it('状态条给出主力还能拿几个交易日', async () => {
    // 运营者 2026-08-19:「我是散户,玻璃 2609 合约 8.31 之前需要离场,
    // 要提前 10 个交易日」。2026-08-14 玻璃主力还是 FG2609、只剩 11 个交易日,
    // 而页面当时对此只字不提 —— 差一天就撞线。
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('还剩 53 个交易日')
    expect(w.text()).not.toContain('必须平仓')
  })

  it('撞线那天必须写「必须平仓」,不能只把数字染个色', async () => {
    stubFetch({ ...PAYLOAD, delivery: { window_end: '2026-08-31', days_left: 10, limit: 10, must_exit: true } })
    const w = mount(HogMoney, { props: { instrument: 'FG' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('必须平仓')
  })

  it('引擎还没跑过的旧 JSON 没有这个字段,页面照常渲染', async () => {
    // 前端先于引擎上线:当晚引擎跑过之前线上还是上一版 JSON。
    // 写成必填会让整页白掉,这一条就是防这个。
    const { delivery: _drop, ...older } = PAYLOAD as Record<string, unknown>
    stubFetch(older)
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('生猪')
    expect(w.text()).not.toContain('还剩')
  })

  it('campaign 品种渲染战役持仓与观察列表(DEC-133)', async () => {
    // 生猪切换为逐合约战役:多仓并行 + 逐合约观察 + 份额资格。
    // 老品种 payload 没有 campaign 字段,上面那条兼容测试盖住;这条盖有它的形状。
    const campaign = {
      ...PAYLOAD,
      rules: { ...PAYLOAD.rules, strategy: 'campaign' },
      signal: { ...PAYLOAD.signal, entry_side: null, entry_blocked: 'LH2611 多:区间累计加仓 543 手,未到 800 手' },
      campaign: {
        params: { add_min: 150, confirm: 800, gap: 3, tail: 10, unload: 0.3, share: 0.25 },
        positions: [{
          side: 'short', entry_date: '2026-08-10', exit_date: null, entry_px: 12155,
          exit_px: null, contract: 'LH2611', ret_pct: -2.7, hold_days: 6, exit_reason: null,
          batch_cost: 12104, camp_net: 11673, camp_peak: 14724, unload_pct: 0.2072
        }],
        watch: [
          { contract: 'LH2611', side: 'long', camp_net: 543, camp_vwap: 12150, zone_add: 543,
            batch_cost: 12150, zone_age: 2, qualified: false, entry_ready: false,
            blocked: '该方向历史战役盈亏未达对侧 25%,非聪明钱侧', settle: 12190, days_left: 40 },
          { contract: 'LH2611', side: 'short', camp_net: 11673, camp_vwap: 12180, zone_add: 6662,
            batch_cost: 12180, zone_age: 1, qualified: true, entry_ready: false,
            blocked: '已持仓', settle: 12190, days_left: 40 }
        ],
        qual: { long_pnl_yi: 1.43, short_pnl_yi: 35.74, long_ok: false, short_ok: true, share: 0.25 },
        note: '逐合约战役:多仓并行。'
      }
    }
    stubFetch(campaign)
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('战役持仓')
    expect(w.text()).toContain('批次成本')
    expect(w.text()).toContain('非聪明钱侧')
    expect(w.text()).toContain('35.74')
    // 机构已卸列:20% /30%走
    expect(w.text()).toContain('21%')
  })

  it('读引擎产出的 JSON 并渲染当前状态', async () => {
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
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
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('未验证')
    expect(w.find('.caveat-box').exists()).toBe(true)
    expect(w.find('.strip-pill').classes()).toContain('unverified')
    w.unmount()
  })

  it('跨过主力换月时,状态条的合约跟着现价走,并说明两个价格不可相减', async () => {
    // 进场在 LH2609 @10825,现价是 LH2611 的 12485,看着像涨 15%,实际 +5.23%。
    // 生猪各合约价差最大 49%,并排摆着不说明,就是在诱导人做减法。
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.strip-name').text()).toContain('LH2611')
    const t = w.text()
    expect(t).toContain('不要相减')
    expect(t).toContain('LH2609')
    w.unmount()
  })

  it('空仓时不渲染状态条,也不出未验证提示', async () => {
    stubFetch({ ...PAYLOAD, state: '观察中', position: null })
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.symbol-strip').exists()).toBe(false)
    expect(w.find('.caveat-box').exists()).toBe(false)
    expect(w.text()).toContain('无持仓')
    w.unmount()
  })

  it('历史页把多空分开统计,做多那栏挂未验证徽标', async () => {
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
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
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[1].trigger('click')
    const t = w.find('.compare').text()
    expect(t).toContain('恒定满仓做空')
    expect(t).toContain('+99.20%')   // 基准累计
    expect(t).toContain('+86.50%')   // 策略累计——比基准低,也要如实显示
    w.unmount()
  })

  it('翻页用 el-pagination,与金银历史信号一致', async () => {
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
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
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
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
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.find('.caveat-box').text()
    expect(t).toContain('做多支路是关闭的')
    // 「减空」不等于「转多」——机构此刻仍是净空,这句必须在
    expect(t).toContain('净空')
    w.unmount()
  })

  it('规则文案全部由 payload 生成,不写死', async () => {
    // 上一版把「每 3 个月」「36 笔」写进模板,引擎改成一年、做多关掉之后页面还在
    // 说旧数字,被运营者当场发现。这条盯住这类回归。
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[3].trigger('click')
    const t = w.text()
    expect(t).toContain('每年')          // reselect_months=12
    expect(t).not.toContain('每 3 个月')
    expect(t).toContain('前 5 家')        // group_k
    expect(t).toContain('持满 40 个交易日')
    expect(t).toContain('反向 1 笔')      // 出场分布数出来的
    expect(t).toContain('持满、消退至今一次没触发过')
    w.unmount()
  })

  it('换月反弹提示(DEC-123):触发时写买次主力,未触发时写差多少;历史逐条带结果', async () => {
    const rb = {
      active: true, main: 'LH2609', days_left: 22, drop20: -11.6, dleft_max: 22, drop_min: 5,
      next: 'LH2611', next_px: 11985, since: '2026-01-01',
      history: [
        { date: '2026-03-31', main: 'LH2605', days_left: 22, drop20: -11.4, next: 'LH2607', next_px: 10905, next_ret20: 3.9, days_seen: 20 },
        { date: '2026-07-30', main: 'LH2609', days_left: 22, drop20: -11.6, next: 'LH2611', next_px: 11985, next_ret20: 4.2, days_seen: 13 }
      ]
    }
    stubFetch({ ...PAYLOAD, roll_bounce: rb })
    let w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    let t = w.find('.caveat-box.roll').text()
    expect(t).toContain('买**次主力 LH2611**')
    expect(t).toContain('2026-03-31')
    expect(t).toContain('+3.90%')
    expect(t).toContain('(13日)')
    expect(t).toContain('不是全样本验证')
    w.unmount()
    stubFetch({ ...PAYLOAD, roll_bounce: { ...rb, active: false, main: 'LH2611', days_left: 53, drop20: -3.8, next: 'LH2701' } })
    w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    t = w.find('.caveat-box.roll').text()
    expect(t).toContain('未触发')
    expect(t).toContain('剩 53 个交易日')
    expect(t).toContain('买次主力 LH2701')
    w.unmount()
    // 没有这块(别的品种 / 旧产物)就不渲染
    stubFetch({ ...PAYLOAD, roll_bounce: null })
    w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.caveat-box.roll').exists()).toBe(false)
    w.unmount()
  })

  it('固定名单(DEC-122)时席位组页写「固定名单」,不写重选与下次', async () => {
    stubFetch({
      ...PAYLOAD, group_mode: 'fixed',
      group_log: [{ date: '2026-08-23', members: ['国泰君安', '东证期货', '东吴期货', '永安期货', '浙商期货'],
                    alpha: { '国泰君安': 4.99, '东证期货': 5.4, '东吴期货': 3.5, '永安期货': 0.92, '浙商期货': 2.14 } }],
      reselect: { last: null, next: null, changed_at: '2026-08-23' }
    })
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[2].trigger('click')
    const t = w.text()
    expect(t).toContain('固定名单')
    expect(t).toContain('永安期货')
    expect(t).not.toContain('下次')
    expect(t).not.toContain('换人历史')
    await w.findAll('.tab')[3].trigger('click')
    expect(w.text()).toContain('固定名单(国泰君安、东证期货、东吴期货、永安期货、浙商期货')
    w.unmount()
  })

  it('组内各家要显示当前主力合约上的持仓成本', async () => {
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('12835')                 // 国泰君安,覆盖完整
    expect(t).toContain('13075(覆盖 1,200 手)')  // 东证,覆盖不全要标出来
    // 东吴当日不在榜:整行就是「当日未上榜」,压根不该去取成本
    expect(t).toContain('当日未上榜')
    // 成本是哪个合约上的,必须说清——生猪各合约价差最大 49%
    expect(t).toContain('LH2611 这一个合约')
    w.unmount()
  })

  it('散户反向维度要显示,并标出与机构是共振还是背离', async () => {
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('散户反向')
    expect(t).toContain('与机构共振')
    expect(t).toContain('散户在减多')      // z=+1.11 → 反向看涨
    expect(t).toContain('6,939')           // 三家合计净多
    expect(t).toContain('东方财富')
    // 它还没参与交易,这句必须在——否则会被当成可执行信号
    expect(t).toContain('不参与进出场')
    w.unmount()
  })

  it('与机构背离时要如实标出', async () => {
    // 散户净多且 5 日在加(change>0)→ 加多;z 为负 → 反向看跌。两半各有来源。
    stubFetch({ ...PAYLOAD, retail: { ...PAYLOAD.retail, resonate: false, z: -0.8, change: 6974 } })
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('与机构背离')
    expect(t).toContain('散户在加多 → 反向看跌')
    w.unmount()
  })

  it('仓位动作按净持仓方向说:净空且在加就是「加空」,不是「减多」', async () => {
    // 2026-08-23 焦煤实况:散户三家净空 −12,454、5 日 −3,018、z +0.26;
    // 机构净多 66,360、5 日 −294、z −0.01。旧文案把前者写成「减多」、后者写成「加空」。
    stubFetch({
      ...PAYLOAD,
      signal: { ...PAYLOAD.signal, net: 66360, change: -294, z: -0.01 },
      retail: { ...PAYLOAD.retail, net: -12454, change: -3018, z: 0.26 }
    })
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('散户在加空 → 反向看涨')
    expect(t).toContain('机构在减多')
    expect(t).not.toContain('散户在减多')
    expect(t).not.toContain('机构在加空')
    w.unmount()
  })

  it('策略方案页要写明现行是方案 C,且不能把它说成「实测最优」', async () => {
    // 三个候选单笔均值差的 t 只有 0.22~0.49,统计上分不出高下。
    // 选 C 是运营者的判断,页面必须如实这么说——把判断包装成「数据证明」是最容易
    // 犯的错,而且几个月后没人记得当初有没有证据。
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('.tab')[3].trigger('click')
    const t = w.text()
    expect(t).toContain('共振')
    expect(t).toContain('分不出高下')
    expect(t).not.toContain('实测最优')
    w.unmount()
  })

  it('按品种读各自的 JSON,不是写死生猪那个', async () => {
    // 三个品种一份组件、三份 payload。写死文件名会让玻璃纯碱显示生猪的数据,
    // 而且页面上一切正常、看不出错。
    const seen: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      seen.push(url)
      if (url.includes('/seats/net-position')) {
        return { ok: true, status: 200, json: async () => NET_POSITION } as Response
      }
      return { ok: true, status: 200, json: async () => PAYLOAD } as Response
    }))
    const w = mount(HogMoney, { props: { instrument: 'FG' as const },
                                global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(seen.some((u) => u.includes('fg_signals.json'))).toBe(true)
    expect(seen.some((u) => u.includes('hog_signals.json'))).toBe(false)
    // 持仓成本那一路也要按品种取,不能永远问生猪
    expect(seen.some((u) => u.includes('/seats/net-position') && u.includes('FG'))).toBe(true)
    w.unmount()
  })

  it('取不到 JSON 时给出错误,而不是白屏', async () => {
    stubFetch(null, false)
    const w = mount(HogMoney, { props: { instrument: 'LH' as const }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('.err').exists()).toBe(true)
    w.unmount()
  })
})
