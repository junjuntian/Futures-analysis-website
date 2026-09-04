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
  days: [],
  // 按接口补齐，不图省事——mock 缺字段会让组件在渲染期读到 undefined，
  // 表现是断言全绿、进程退出码却是 1（2026-08-18 踩过）。
  latest_trade_date: null,
  latest_members: []
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
          // 名录里放一家能用拼音首字母找的，拼音检索那条测试盯着它。
          members: ['中信', '国泰君安', '高盛期货'],
          trade_date: '2026-08-14',
          available_dates: ['2026-08-14'],
          coverage_start: '2010-01-04',
          rows: [],
          costs: []
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
    // 席位要显式给：一家没勾时净持仓子页整个不渲染（2026-08-19 起清空不再
    // 自动补名录第一家），合约选择器也就无从谈起。
    localStorage.setItem('seats.members', '中信')
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
    localStorage.setItem('seats.members', '中信')
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

describe('席位持仓表的成本列', () => {
  beforeEach(() => {
    localStorage.clear()
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    stubFetch()
    // 在通用桩之上,给「中信」这一家一张带成本的持仓表。
    const base = fetch as unknown as (input: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = decodeURIComponent(input.toString())
        if (url.includes('/seats/positions') && url.includes('member=中信')) {
          const row = (contract: string, rank_type: 'long' | 'short', quantity: string) => ({
            exchange: 'DCE', instrument: 'LH', contract, is_variety_total: false,
            variety_total_is_computed: false, rank_type, rank: 1, member: '中信',
            quantity, change: '5', source: 'official'
          })
          return response({
            member: '中信', instrument: null, members: ['中信'], trade_date: '2026-08-21',
            available_dates: ['2026-08-21'], coverage_start: '2023-08-11',
            rows: [row('LH2701', 'long', '2015'), row('LH2705', 'long', '980'), row('LH2705', 'short', '796')],
            costs: [
              { instrument: 'LH', contract: 'LH2701', net_position: '2015', cost: '13780.5', cost_unknown_reason: null },
              // 这个合约那天不在榜:成本是「不知道」,不是 0 —— 界面必须说原因
              { instrument: 'LH', contract: 'LH2705', net_position: null, cost: null, cost_unknown_reason: 'seat_off_the_board' }
            ]
          })
        }
        return base(input)
      })
    )
  })

  it('每个合约后面摆它自己的净持仓成本,并写清方向与手数;不可知的说原因', async () => {
    localStorage.setItem('seats.members', '中信')
    const wrapper = mountPage()
    await flushPromises()
    wrapper.findComponent({ name: 'ElRadioGroup' }).vm.$emit('update:modelValue', 'positions')
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('13,780.5')
    // 2026-09-04 措辞与品种汇总那排对齐(运营者:「跟这个品种汇总一样」)。
    // 这两个数本来就是同一件事:引擎净持仓计价,一个合约一个净额成本,按净额正负
    // 归入多单/空单桶,品种汇总的「多 N 手 均价 X」就是这一列加权出来的。
    // 原来写「净多 2,015 手」,看着像和汇总那排是两回事。
    expect(text).toContain('多单均价 · 2,015 手')
    expect(text).not.toContain('净多 2,015 手')
    // LH2705 那行:成本空,原因要露出来,不能只画个横杠
    expect(text).toContain('不在前 20 榜上')
    wrapper.unmount()
  })
})

describe('席位多选框', () => {
  beforeEach(() => {
    localStorage.clear()
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().csrfToken = 'csrf-test'
    stubFetch()
  })

  it('交易所未公布排名的日子(DEC-130):页头说明写出天数与 2 万手规则,与掉榜分开', async () => {
    const day = (trade_date: string, extra: Record<string, unknown>) => ({
      trade_date, open_price: '100', high_price: '101', low_price: '99', close_price: '100',
      net_position: '0', long_lots: '0', short_lots: '0', counted_members: [], missing_members: ['中信'],
      inferred_members: [], daily_pnl: null, cumulative_pnl: '0', long_cost: null, short_cost: null,
      long_cost_lots: '0', short_cost_lots: '0', long_lots_total: '0', short_lots_total: '0', ...extra
    })
    const building = {
      ...BUILDING, contract: 'AU2612', is_variety_total: false,
      days: [
        day('2026-06-22', { counted_members: ['中信'], missing_members: [], net_position: '-100', short_lots: '100' }),
        day('2026-06-23', { counted_members: ['中信'], missing_members: [], net_position: '-100', short_lots: '100' }),
        day('2026-06-24', { unpublished: true }),
        day('2026-06-25', { unpublished: true })
      ]
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.includes('/seats/member-instruments')) return response({ member: '中信', instruments: ['AU'] })
      if (url.includes('/seats/net-position')) return response(building)
      if (url.includes('/seats/favorites')) return response([])
      if (url.includes('/seats/positions')) return response({ member: '中信', instrument: null, members: ['中信'], trade_date: '2026-08-14', available_dates: ['2026-08-14'], coverage_start: '2010-01-04', rows: [], costs: [] })
      return response({})
    }))
    localStorage.setItem('seats.members', '中信')
    localStorage.setItem('seats.instrument', 'AU')
    localStorage.setItem('seats.contract', 'AU2612')
    const wrapper = mountPage()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('2 天交易所没有公布这个合约的持仓排名')
    expect(text).toContain('2 万手')
    // 未公布日不算掉榜:掉榜那句不该因为这两天而出现
    expect(text).not.toContain('2 天至少有一家掉出交易所前 20 榜')
    wrapper.unmount()
  })

  it('一家都没勾时不自动补上名录第一个，而是让人自己选', async () => {
    // 清空是「我要重挑」。替他塞一家回去，他刚腾出来的框又满了；
    // 配上加载期间禁用输入，「空着且能搜」这个状态就根本不存在了。
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    // 从没被写过（初始空数组不算变化），更没有被塞进一个名录第一家。
    expect(localStorage.getItem('seats.members')).toBeFalsy()
    expect(wrapper.text()).toContain('先在上面选几个席位')
    // 也没有为不存在的选择去打持仓接口。
    const calls = (fetch as unknown as { mock: { calls: Array<[RequestInfo | URL]> } }).mock.calls
      .map(([input]) => input.toString())
    expect(calls.some((url) => url.includes('/seats/positions') && url.includes('member='))).toBe(
      false
    )
    wrapper.unmount()
  })

  it('中文输入法预编辑期间敲 gs 也能筛出高盛', async () => {
    // 这条盯的是 el-select 的 handleQueryChange 里那句
    // `if (… || isComposing.value) return`——IME 预编辑期间它根本不调
    // filter-method。而拼音搜索的主力用法恰恰是**永远不选字**：敲 gs 要的就是
    // 这两个字母，composition 永不结束，查询就永远是空的。
    // pinyin.test.ts 全绿也挡不住这个，因为坏的不是拼音表而是事件链路。
    localStorage.setItem('seats.members', '中信')
    const wrapper = mountPage()
    await flushPromises()
    await flushPromises()

    const select = wrapper.findAllComponents({ name: 'ElSelect' })[0]
    const input = select.find('input')
    await input.trigger('click')
    await flushPromises()

    // 中文输入法：compositionstart 之后每敲一个字母来一次 compositionupdate，
    // 不选字就永远等不到 compositionend。
    await input.trigger('compositionstart')
    ;(input.element as HTMLInputElement).value = 'gs'
    await input.trigger('compositionupdate')
    await flushPromises()

    const options = select.findAllComponents({ name: 'ElOption' })
    const labels = options.map((option) => option.props('label') as string)
    expect(labels).toContain('高盛期货')
    expect(labels).not.toContain('中信')
    wrapper.unmount()
  })
})
