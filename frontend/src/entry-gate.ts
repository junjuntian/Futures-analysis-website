/**
 * 「进场条件」到底在比哪个数(2026-08-20 修)。
 *
 * 运营者问「玻璃为什么没有触发多单进场条件」:页面写着「信号强度需达 1(现 2.09)」,
 * 2.09 明明大于 1,却显示无持仓——**页面自相矛盾**。
 *
 * 根因:现行策略是**方案 C**(`signal_source = "resonance"`,DEC-086),引擎的进场
 * 判据比的是**共振后的散户信号**(`retail.z`),而页面显示的是**机构合计流向**
 * (`signal.z`)。当天机构 2.09、散户 0.92 —— 真正被测的那个数差 0.08 没到线。
 *
 * 为什么会显示错:`HogPayload.retail` 的类型注释一直写着「当前只作展示,不参与
 * 进出场」。那句话在切到方案 C 之后就过期了,而引擎 payload 里的 `retail.note`
 * 明写「现行策略(方案 C)就是用它进出场」。**两处对同一件事的说法相反,页面
 * 跟了错的那一处。**
 *
 * 这个模块只回答一件事:**这一行该把哪个数摆出来**。抽出来是为了能测——
 * 埋在 `.vue` 里的表达式错了不会报错,只会安静地显示一个说服力十足的错数字。
 */

/** 判据需要的那几个字段。用结构类型,免得把整个 payload 类型拖进来。 */
export interface EntryGateInput {
  signal: { z: number | null; enter: number }
  retail: {
    z: number | null
    /** 与机构流向是否同号。方案 C 下不同号就进不了场,与强度无关。 */
    resonate: boolean
    /** 这一路是否**真的在参与进出场**(= `signal_source === 'resonance'`)。 */
    trades: boolean
  }
}

export interface EntryGate {
  /** 真正被拿去和门槛比的那个数。共振不成立时为 `null` —— 那时它没有意义。 */
  value: number | null
  threshold: number
  /** 这个数来自哪一路。 */
  source: 'retail' | 'flow'
  /** 机构与散户背离:方案 C 下直接判死,再大也不进场。 */
  divergent: boolean
  /** 门槛达成没有。做多做空对称,所以比的是绝对值。 */
  met: boolean
}

export function entryGate(data: EntryGateInput): EntryGate {
  const threshold = data.signal.enter
  // 不走方案 C 时(`signal_source = "flow"`),进场就是拿机构信号比门槛,
  // 原来的显示是对的。
  if (!data.retail.trades) {
    const value = data.signal.z
    return {
      value,
      threshold,
      source: 'flow',
      divergent: false,
      met: value !== null && Math.abs(value) >= threshold
    }
  }
  if (!data.retail.resonate) {
    return { value: null, threshold, source: 'retail', divergent: true, met: false }
  }
  const value = data.retail.z
  return {
    value,
    threshold,
    source: 'retail',
    divergent: false,
    met: value !== null && Math.abs(value) >= threshold
  }
}

/**
 * 写成人话。**必须点名比的是哪一路**,否则读者会拿旁边那个更大的机构数字对照,
 * 得出「条件已满足却没进场」的结论——这正是这次要修的那个误会。
 */
export function entryGateText(data: EntryGateInput): string {
  const gate = entryGate(data)
  const flow = data.signal.z
  const shown = (v: number | null) => (v === null ? '—' : v.toFixed(2))

  if (gate.source === 'flow') {
    return `机构合计流向需达 ${gate.threshold}(现 ${shown(gate.value)})`
  }
  if (gate.divergent) {
    return `机构与散户背离,不进场(机构 ${shown(flow)} · 散户 ${shown(data.retail.z)})`
  }
  const head = `共振后的散户信号需达 ${gate.threshold}(现 ${shown(gate.value)})`
  // 机构那个数就摆在旁边的卡片里,不一起说清楚,读者一定会拿它去对门槛。
  return gate.met
    ? `${head} —— 已达标,次日开盘进场`
    : `${head};机构 ${shown(flow)} 不是这里比的那个数`
}
