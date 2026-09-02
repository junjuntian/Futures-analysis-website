export interface ApiEnvelope<T> {
  data: T
  meta: { request_id: string }
}

interface ApiErrorBody {
  code?: string
  message?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string
  readonly data?: unknown

  constructor(status: number, code: string, message: string, requestId?: string, data?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.data = data
  }
}

export function isAuthorizationError(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.status === 401 || error.status === 403)
}

export interface HealthStatus { status: string; checked_at: string }
export interface VersionInfo { name: string; version: string; git_sha: string }
export interface UserSummary { id: string; username: string; roles: string[]; permissions: string[] }
export interface WorkspaceSummary { id: string; name: string }
export interface MeResponse { user: UserSummary; workspace: WorkspaceSummary }
export interface SessionSummary {
  id: string
  current: boolean
  created_at: string
  last_seen_at: string
  absolute_expires_at: string
  idle_expires_at: string
  user_agent?: string | null
}

export type SpreadProviderResultKind = 'ok' | 'empty'

export type SpreadProvider = 'sanhe' | 'self'

export interface SpreadSourceMetadata {
  provider: SpreadProvider
  source_code: string
  source_display_name: string
  // 自研那条是 derived：我们自己算的，不是从哪个聚合商取来的。
  source_type: 'aggregator' | 'derived'
  fetched_at: string
  data_cutoff_at?: string | null
  // 三禾给的是算好的价差；自研是两腿收盘价相减。
  price_basis: 'upstream_spread' | 'own_close_difference'
  // 自研这条两条腿的收盘价都在我们自己库里，所以是 true。
  raw_leg_prices_available: boolean
  provider_algorithm_version: string
}

export interface SpreadVariety {
  market: string
  name: string
  symbol: string
}

export interface SpreadVarietiesResponse {
  source: SpreadSourceMetadata
  items: SpreadVariety[]
  result_kind: SpreadProviderResultKind
}

export interface SpreadMonthsResponse {
  source: SpreadSourceMetadata
  variety: string
  months: string[]
  basis?: number | null
  basis_semantics_confirmed: false
  result_kind: SpreadProviderResultKind
}

export interface FreeSpreadLeg {
  variety: string
  symbol: string
  month: string
}

export interface FreeSpreadQueryRequest {
  provider: SpreadProvider
  leg1: FreeSpreadLeg
  leg2: FreeSpreadLeg
}

export interface ContinuousSpreadPoint {
  trade_date: string
  value: number
  from_code: string
  to_code: string
  segment_no: number
}

export interface SpreadSegmentBoundary {
  segment_no: number
  trade_date: string
  from_code: string
  to_code: string
  previous_from_code?: string | null
  previous_to_code?: string | null
  reason: string
}

export interface SeasonalYearSeries {
  year: number
  values: Array<number | null>
  sample_count: number
  missing_count: number
  segment_nos: number[]
  rule_version: string
  sample_start?: string | null
  sample_end?: string | null
}

export interface MonthlyCell {
  month: number
  delta?: number | null
  sample_count: number
  is_partial: boolean
}

export interface SpreadAnalysisTrace {
  provider: SpreadProvider
  source_code: string
  data_cutoff_at?: string | null
  price_basis: 'upstream_spread'
  sample_start?: string | null
  sample_end?: string | null
  sample_count: number
  excluded_point_count: number
  calendar_version_ids: string[]
  window_algorithm_version: string
  statistics_algorithm_version: string
  rule_version: string
}

export interface FreeSpreadQueryResponse {
  series_id: string
  source: SpreadSourceMetadata
  query: FreeSpreadQueryRequest
  quality: {
    status: 'ok' | 'partial' | 'empty'
    input_point_count: number
    retained_point_count: number
    excluded_point_count: number
    missing_contract_point_count: number
  }
  algorithm_versions: {
    provider: string
    window: string
    statistics: string
    rule: string
  }
  continuous_series: {
    trace: SpreadAnalysisTrace
    points: ContinuousSpreadPoint[]
    segment_boundaries: SpreadSegmentBoundary[]
    current_value?: number | null
  }
  seasonal_series: {
    trace: SpreadAnalysisTrace
    axis: string[]
    years: SeasonalYearSeries[]
    current_year?: number | null
  }
  monthly_matrix: {
    trace: SpreadAnalysisTrace
    years: Array<{ year: number; months: MonthlyCell[] }>
    up_ratios: Array<{
      month: number
      ratio?: number | null
      positive_year_count: number
      eligible_year_count: number
    }>
  }
  segments: Array<{
    segment_no: number
    window_year?: number | null
    from_code: string
    to_code: string
    candidate_start: string
    candidate_end: string
    window_start?: string | null
    window_end?: string | null
    calendar_version_ids: string[]
    retained_point_count: number
    excluded_point_count: number
    boundary_reason: string
  }>
}

export interface SpreadFavorite {
  id: string
  name: string
  provider: SpreadProvider
  leg1: FreeSpreadLeg
  leg2: FreeSpreadLeg
  created_at: string
}

async function parseApiError(response: Response, fallback: string): Promise<ApiError> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    payload = undefined
  }
  const envelope = payload as Partial<ApiEnvelope<ApiErrorBody>> | undefined
  const body = envelope?.data
  const code = body?.code ?? `http_${response.status}`
  return new ApiError(
    response.status,
    code,
    body?.message ?? fallback,
    envelope?.meta?.request_id,
    body && !body.code ? body : payload
  )
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'include' })
  if (!response.ok) throw await parseApiError(response, `request failed: ${response.status}`)
  return response.json() as Promise<T>
}

export async function sendJson<T>(
  path: string,
  body: unknown,
  csrfToken?: string,
  method = 'POST',
  extraHeaders: Record<string, string> = {}
): Promise<T> {
  const headers: Record<string, string> = { 'content-type': 'application/json', ...extraHeaders }
  if (csrfToken) headers['x-csrf-token'] = csrfToken
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'include',
    body: JSON.stringify(body ?? {})
  })
  if (!response.ok) throw await parseApiError(response, `request failed: ${response.status}`)
  return response.json() as Promise<T>
}

export function getSpreadVarieties(
  provider: SpreadProvider = 'self'
): Promise<ApiEnvelope<SpreadVarietiesResponse>> {
  return getJson(`/api/v1/spread-analytics/providers/${provider}/varieties`)
}

export function getSpreadMonths(
  variety: string,
  provider: SpreadProvider = 'self'
): Promise<ApiEnvelope<SpreadMonthsResponse>> {
  return getJson(
    `/api/v1/spread-analytics/providers/${provider}/varieties/${encodeURIComponent(variety)}/months`
  )
}

export function queryFreeSpread(
  request: FreeSpreadQueryRequest,
  csrfToken: string
): Promise<ApiEnvelope<FreeSpreadQueryResponse>> {
  return sendJson('/api/v1/spread-analytics/free-spread/query', request, csrfToken)
}

export function getSpreadFavorites(): Promise<ApiEnvelope<SpreadFavorite[]>> {
  return getJson('/api/v1/spread-analytics/favorites')
}

export function createSpreadFavorite(
  request: { name: string; provider: SpreadProvider; leg1: FreeSpreadLeg; leg2: FreeSpreadLeg },
  csrfToken: string
): Promise<ApiEnvelope<SpreadFavorite>> {
  return sendJson('/api/v1/spread-analytics/favorites', request, csrfToken)
}

export async function deleteSpreadFavorite(favoriteId: string, csrfToken: string): Promise<void> {
  const response = await fetch(`/api/v1/spread-analytics/favorites/${encodeURIComponent(favoriteId)}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: { 'x-csrf-token': csrfToken }
  })
  if (!response.ok) throw await parseApiError(response, `delete favorite failed: ${response.status}`)
}

// —— 席位净持仓：几家席位合起来看 ——

export interface NetPositionDay {
  trade_date: string
  open_price: string | null
  high_price: string | null
  low_price: string | null
  close_price: string | null
  /** 所选席位当天的合计净持仓。**只含当天在榜的那几家**，见 `missing_members`。 */
  net_position: string
  /** 当天净多的那些「席位×合约」，手数相加。分腿口径同建仓过程的合约汇总。 */
  long_lots: string
  short_lots: string
  counted_members: string[]
  /**
   * 当天掉出前二十的席位：持仓**未知**，没有计进合计。
   *
   * 界面必须把这件事说出来。看的人以为合计覆盖了他选的全部席位，而那天少了一家，
   * 曲线上就是一段无缘无故的下台阶。
   */
  missing_members: string[]
  /** 交易所当天**没有公布**这个合约(或品种)的持仓排名(DEC-130):大商所只对持仓量
   *  ≥ 2 万手的合约发排名,合约临近到期跌破 2 万手后停发。不是席位掉榜,是整张榜不存在;
   *  页面上分开说,净持仓留空不画 0。老产物没有这个字段按 false 读。 */
  unpublished?: boolean
  /** 当天按回榜反推值计入合计的席位：实际未上榜，数字是倒推的。 */
  inferred_members: string[]
  /** 当日盈亏 =（今结算 − 昨结算）× 昨净持仓 × 点值，逐「席位×合约」各算各的再相加。
   * 掉榜或无结算价的那天为 null：那天赚了多少不知道，不是零。 */
  daily_pnl: string | null
  /** 当日盈亏的逐日累加。不可知的天按 0 计入，累计线不断开。 */
  cumulative_pnl: string
  /** 净多那几条腿的加权成本（推算）；`long_cost_lots` 是它覆盖到的手数。 */
  long_cost: string | null
  long_cost_lots: string
  short_cost: string | null
  short_cost_lots: string
}

/**
 * 最新一天里某一家的多空手数与均价。摘要下面那排逐家的数就是它。
 *
 * 与合计同源：后端按 member 分组各跑一遍同一套成本引擎，所以这排加起来
 * 必然对得上合计那排。
 */
export interface MemberLeg {
  member: string
  long_lots: string
  long_cost: string | null
  long_cost_lots: string
  short_lots: string
  short_cost: string | null
  short_cost_lots: string
  /** 这家当天不在榜：持仓**未知**，不是零。手数字段此时是 0，别拿去显示。 */
  missing: boolean
  /** 这家当天的持仓由回榜日增减倒推得出。 */
  inferred: boolean
}

export interface SeatNetPositionResponse {
  instrument: string
  contract: string | null
  is_variety_total: boolean
  /** 去重后的所选席位。 */
  members: string[]
  all_members: string[]
  contracts: string[]
  price_series_kind: 'open_interest_weighted' | 'dominant_unadjusted' | null
  /** 合约点值。盈亏由它乘出来，界面把它写在明面上。库里没配就是 `null`，此时不算盈亏。 */
  price_multiplier: string | null
  days: NetPositionDay[]
  /** 逐家×逐日的多空手数与成本(DEC-132),legs 与 days 按下标对齐;掉榜/未公布日为 null。
   *  字段名故意短(l/lc/s/sc):十家×几千天,长名字白白翻倍体积。老产物没有这个字段。 */
  member_series?: Array<{
    member: string
    legs: Array<null | { l: string; lc: string | null; s: string; sc: string | null }>
  }>
  /** `days` 最后一天的日期，由后端一并给出，免得两边各判一次「哪天算最新」。 */
  latest_trade_date: string | null
  latest_members: MemberLeg[]
}

export interface SeatFavorite {
  id: string
  name: string
  members: string[]
}

export function getSeatNetPosition(options: {
  instrument: string
  members: string[]
  contract?: string
  /** 看到哪一天为止(含当天)。不传＝看到最新。席位页两个子页共用同一个交易日。 */
  tradeDate?: string
}): Promise<ApiEnvelope<SeatNetPositionResponse>> {
  const params = new URLSearchParams()
  params.set('instrument', options.instrument)
  params.set('members', options.members.join(','))
  if (options.contract) params.set('contract', options.contract)
  if (options.tradeDate) params.set('trade_date', options.tradeDate)
  return getJson(`/api/v1/spread-analytics/seats/net-position?${params.toString()}`)
}

/** 盈亏商品(DEC-157):区间逐日盯市盈亏。member 模式=该席位逐品种;instrument 模式=该品种逐席位。 */
export interface PnlBreakdownItem {
  key: string
  pnl: string
  known_days: number
  filled_days: number
  no_multiplier: boolean
}

export interface SeatPnlResponse {
  mode: 'member' | 'instrument'
  member: string | null
  instrument: string | null
  start_date: string
  end_date: string
  items: PnlBreakdownItem[]
  all_instruments: string[]
}

export function getSeatPnlBreakdown(options: {
  member?: string
  instrument?: string
  startDate: string
  endDate: string
}): Promise<ApiEnvelope<SeatPnlResponse>> {
  const params = new URLSearchParams()
  if (options.member) params.set('member', options.member)
  if (options.instrument) params.set('instrument', options.instrument)
  params.set('start_date', options.startDate)
  params.set('end_date', options.endDate)
  return getJson(`/api/v1/spread-analytics/seats/pnl-breakdown?${params.toString()}`)
}

export function getSeatFavorites(): Promise<ApiEnvelope<SeatFavorite[]>> {
  return getJson('/api/v1/spread-analytics/seats/member-favorites')
}

export function createSeatFavorite(
  request: { name: string; members: string[] },
  csrfToken: string
): Promise<ApiEnvelope<SeatFavorite>> {
  return sendJson('/api/v1/spread-analytics/seats/member-favorites', request, csrfToken)
}

export async function deleteSeatFavorite(favoriteId: string, csrfToken: string): Promise<void> {
  const response = await fetch(
    `/api/v1/spread-analytics/seats/member-favorites/${encodeURIComponent(favoriteId)}`,
    {
      method: 'DELETE',
      credentials: 'include',
      headers: { 'x-csrf-token': csrfToken }
    }
  )
  if (!response.ok) {
    throw await parseApiError(response, `delete seat favorite failed: ${response.status}`)
  }
}

export interface SeatPositionRow {
  exchange: string
  instrument: string
  contract: string | null
  is_variety_total: boolean
  variety_total_is_computed: boolean
  rank_type: 'volume' | 'long' | 'short'
  rank: number | null
  member: string
  quantity: string
  change: string | null
  source: string
}

/**
 * 席位持仓表里某个合约在所选交易日的**净持仓成本（推算）**（2026-08-22 运营者要求
 * 摆在多头/空头后面）。与净持仓子页同一个引擎算出的同一个数。净持仓计价、多空不分开，
 * 所以一个合约一个成本。`cost` 为 null 时看 `cost_unknown_reason`——那是「不知道」，不是 0。
 */
export interface SeatContractCost {
  instrument: string
  contract: string
  net_position: string | null
  cost: string | null
  cost_unknown_reason: string | null
}

export interface SeatPositionsResponse {
  member: string | null
  instrument: string | null
  /** 有过持仓的会员名录，供顶部选择器使用。 */
  members: string[]
  trade_date: string | null
  available_dates: string[]
  /** 该品种席位数据的最早一天。各品种起点相差十几年，界面必须说出来。 */
  coverage_start: string | null
  rows: SeatPositionRow[]
  /** `rows` 里每个合约的净持仓成本。没选会员时为空。 */
  costs: SeatContractCost[]
}

export function getSeatPositions(options: {
  member?: string
  instrument?: string
  tradeDate?: string
}): Promise<ApiEnvelope<SeatPositionsResponse>> {
  const params = new URLSearchParams()
  if (options.member) params.set('member', options.member)
  if (options.instrument) params.set('instrument', options.instrument)
  if (options.tradeDate) params.set('trade_date', options.tradeDate)
  return getJson(`/api/v1/spread-analytics/seats/positions?${params.toString()}`)
}

export interface BuildingDay {
  trade_date: string
  open_price: string | null
  high_price: string | null
  low_price: string | null
  close_price: string | null
  settlement_price: string | null
  /** `null` = 那天该席位掉出了交易所前 20 榜，持仓**未知**，不是零。 */
  long_position: string | null
  short_position: string | null
  net_position: string | null
  /** 净持仓成本（推算）——由公开持仓变化与结算价推出，不是成交均价。 */
  cost: string | null
  daily_pnl: string | null
  /** 自序列开头至今的当日盈亏累计。不可知的天按 0 计入，累计线不断开。 */
  cumulative_pnl: string
  open_pnl: string | null
  cost_unknown_reason: string | null
  /** 品种汇总档才有：当日的多空两腿。单合约档为 null。 */
  legs: VarietyLegs | null
  /** 该日持仓含回榜反推成分：他实际未上榜，数字由回榜日的增减倒推。 */
  inferred: boolean
}

/**
 * 品种汇总当日的两腿。**多空是按合约的净方向分的组**——净多的那些合约算「多单」，
 * 净空的算「空单」，两者相减才是净持仓。与多头榜/空头榜不是一回事。
 */
export interface VarietyLegs {
  long_lots: string
  /** 净多那些合约的净持仓成本，按手数加权。 */
  long_cost: string | null
  /** `long_cost` 覆盖到的手数。小于 `long_lots` 说明有合约成本不可知。 */
  long_cost_lots: string
  short_lots: string
  short_cost: string | null
  short_cost_lots: string
}

export interface SeatBuildingResponse {
  instrument: string
  member: string
  contract: string | null
  is_variety_total: boolean
  price_multiplier: string | null
  members: string[]
  /** 该会员在该品种上历史持有过的全部合约，新月份在前。不随所选交易日变化。 */
  contracts: string[]
  /**
   * 汇总档 K 线的口径。单合约档为 `null`——那是合约自己的真实行情。
   *
   * 汇总档画的是**合成价**，不是任何一个合约的真实成交价。界面必须写明是哪一种，
   * 否则看的人会拿这个价位去定止损。
   */
  price_series_kind: 'open_interest_weighted' | 'dominant_unadjusted' | null
  days: BuildingDay[]
}

export interface MemberInstrumentsResponse {
  member: string
  /** 该席位历史上持有过的全部品种。建仓过程的品种下拉用它，不随所选日期变化。 */
  instruments: string[]
}

export function getSeatMemberInstruments(
  member: string
): Promise<ApiEnvelope<MemberInstrumentsResponse>> {
  return getJson(
    `/api/v1/spread-analytics/seats/member-instruments?member=${encodeURIComponent(member)}`
  )
}

export function getSeatBuilding(
  instrument: string,
  member: string,
  contract?: string
): Promise<ApiEnvelope<SeatBuildingResponse>> {
  const params = new URLSearchParams({ instrument, member })
  if (contract) params.set('contract', contract)
  return getJson(`/api/v1/spread-analytics/seats/building?${params.toString()}`)
}

// —— 总览页「黄金白银报告表」 ——
//
// 一张表两个来源：上半（压力位/支撑位）是运营者手填的盘面判断，下半（席位净持仓
// 与筹码）从事实表现算。两半分别读写。

/** 一个品种上的昨 / 今净持仓与筹码。`null` = 那天不在榜上，**不是零**。 */
export interface ReportSeatCell {
  previous_net: string | null
  /** previous_net 含反推成分:该席位前一日掉榜,值由今日「持仓−增减」反推。 */
  previous_net_inferred: boolean
  net: string | null
  /** 筹码 = 净持仓成本（推算），与席位页同一个引擎算出的同一个数。合计行为 null。 */
  cost: string | null
}

export interface ReportSeatRow {
  label: string
  members: string[]
  /** 合计行（机构持仓 / 外资持仓 / 散户席位）。 */
  is_total: boolean
  gold: ReportSeatCell
  silver: ReportSeatCell
}

export interface ReportSeatGroup {
  /** `institution` / `watch` / `foreign` / `retail` */
  group_key: string
  members: string[]
}

/** 压力位网格的一行。`values` 依次对应三列行情。 */
export interface ReportLevelRow {
  key: string
  values: string[]
  /** `up` / `down` / `''`。「星级评分度」那一行不用。 */
  bias?: string
  /** 关注度星数 0~5。「星级评分度」那一行改用 `text`。 */
  stars?: number | null
  text?: string
}

export interface OverviewReportResponse {
  trade_date: string
  levels: { rows: ReportLevelRow[] } | null
  /** `levels` 实际来自哪一天。早于 trade_date 说明是沿用上次填的。 */
  levels_source_date: string | null
  seat_groups: ReportSeatGroup[]
  rows: ReportSeatRow[]
}

export function getOverviewReport(tradeDate?: string): Promise<ApiEnvelope<OverviewReportResponse>> {
  const params = new URLSearchParams()
  if (tradeDate) params.set('trade_date', tradeDate)
  const query = params.toString()
  return getJson(`/api/v1/overview/report${query ? `?${query}` : ''}`)
}

/**
 * 这两个写端点回 204，没有响应体，所以不能走 `sendJson`——它末尾无条件
 * `response.json()`，遇到空体会抛「Unexpected end of JSON input」，
 * 保存明明成功了界面却报错。
 */
async function putNoContent(path: string, body: unknown, csrfToken: string): Promise<void> {
  const response = await fetch(path, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', 'x-csrf-token': csrfToken },
    credentials: 'include',
    body: JSON.stringify(body)
  })
  if (!response.ok) throw await parseApiError(response, `request failed: ${response.status}`)
}

export function saveOverviewReportLevels(
  tradeDate: string,
  rows: ReportLevelRow[],
  csrfToken: string
): Promise<void> {
  return putNoContent(
    '/api/v1/overview/report/levels',
    { trade_date: tradeDate, cells: { rows } },
    csrfToken
  )
}

/** 写/清一条套利月份模板的产业备注。note 传空串即删除。 */
export function saveSpreadTemplateNote(
  request: {
    instrument_1: string
    month_1: number
    instrument_2: string
    month_2: number
    note: string
  },
  csrfToken: string
): Promise<void> {
  return putNoContent('/api/v1/spread-analytics/monitor/template-note', request, csrfToken)
}

export function saveOverviewReportSeatGroups(
  groups: ReportSeatGroup[],
  csrfToken: string
): Promise<void> {
  return putNoContent('/api/v1/overview/report/seat-groups', { groups }, csrfToken)
}

/** 套利监控里一条口径轨。历年轨用的是百分位区间，位置可以落在 0~1 之外。 */
export interface SpreadMonitorTrack {
  low: string
  high: string
  position: string | null
  days: number | null
  /** 'high' | 'low' | null。按请求里的阈值算的，不是存下来的。 */
  alert: string | null
}

/** 该月份组合模板在**可交易窗口**内、按日历位置对齐的历年表现。
 *
 * **不是这一组合自己的胜率**:样本是同品种、同月份对、同年差的模板跨年拼起来的
 * (例如鸡蛋 09-01)。一个具体合约对一辈子只有一个生命周期，算不出有意义的比率。
 *
 * 口径:可交易窗口照 5A 窗口引擎(止点 = 先到期那条腿的散户最后交易日);历年按
 * **月-日**对齐,一直看到各自窗口的止点;**曾经触及**即算回归,不比终点。只用
 * 已走完的年份实例。
 *
 * **单看 rate 会骗人**,所以三个数一起给:剩余期一长 rate 就趋近 100%;
 * JD2612/JD2701 的 rate 是 100% 而 drift 中位 −166 点,方向是反的。 */
/** 机构资金里走「合计流向」这套信号的品种。**加品种改这一处**,
 *  组件与视图都从这里取,不各写一份 union。金银是另一套信号,不在里面。 */
export type FlowCode = 'LH' | 'FG' | 'SA' | 'JD' | 'JM' | 'I'

export interface SpreadRevertStats {
  side: string
  /** 曾经触及回归的年数 / 样本年数。不设年数门槛，薄不薄由界面写出来。 */
  hit: number
  n: number
  rate: string
  /** 最有利那一刻相对起点走了多少**点**(择时平仓的上限)。价差会跨零，不给百分比。 */
  move_points: string | null
  /** 一直持到窗口止点的净变化，已标准化成**正数 = 朝回归走**。 */
  drift_points: string | null
  /** 历年 MAE 中位：浮亏到这里是历年常态 —— 补仓参考。 */
  mae_points: string | null
  /** 历年 MAE 最大：风险预留。仓位 = 可承受亏损 ÷ (此数 × 点值)。 */
  mae_max_points: string | null
  /** 历年剩余交易日中位数。 */
  days: number | null
}

/** 平台位阶梯里的一档(DEC-095)。档位/区间/触碰是库里的事实,
 *  偏移、z、到达概率、卖点/止损是读时算的。 */
export interface SpreadShelf {
  level: string
  /** 并档区间两端。链式合并会并出跨几十点的档,只报均值是假精度。 */
  lo: string
  hi: string
  /** 收盘落在该档 ±25 点内的独立回合数。 */
  touches: number
  /** 相对现价差的点数,**正 = 在上方**。 */
  offset: string
  /** 距离 ÷ (σ√剩余交易日)。 */
  z: string | null
  /** 到达概率(%)。**不含方向判断**,逐年离散很大(z=1 时 17%~53%)。 */
  reach_pct: number | null
  /** 'target' | 'stop' | ''。 */
  role: string
}

export interface SpreadMonitorItem {
  trade_date: string
  instrument_1: string
  contract_1: string
  instrument_2: string
  contract_2: string
  is_cross_variety: boolean
  spread: string
  pair: SpreadMonitorTrack
  years: SpreadMonitorTrack | null
  alert: string | null
  /** 今天触发、前一交易日按同一阈值不触发 —— 刚进极值。持续触发的段为 false，
   * 前一日位置缺失时也为 false(判不了就不标)。 */
  is_new_alert: boolean
  /** 未触发且未拐头、或样本不足时为 null。**拐头侧优先**，否则按报警侧(DEC-088)
   * —— ⚡ 由拐头触发，资格与方向就得锚在同一侧。`side` 即交易方向:
   * 'high' = 做空价差，'low' = 做多价差。 */
  revert: SpreadRevertStats | null
  /** 平台位阶梯(DEC-095),按档位从高到低。空数组 = 那天还没算出档位。 */
  shelves: SpreadShelf[]
  /** 报警侧与拐头侧**方向相反**时，另一侧(报警侧)的统计；一致时为 null。
   * 有值 = 这一行的两条轨在讲相反的故事，界面必须摊开给人看。 */
  revert_alt: SpreadRevertStats | null
  /** 'high' | 'low' | null —— 已拐头：近 20 个交易日内当年轨曾进 3% 报警带，
   * 且当前已自极值回撤超过区间宽度的 10%。分层规则的进场信号（DEC-063）。 */
  turn: string | null
  /** 今天刚拐头：位置是今天才穿过回撤线的。拐头标最多挂 20 个交易日，
   * 「可进场状态」与「今天就是进场日」是两回事，这个字段点亮后者。 */
  is_new_turn: boolean
  /** 拐头侧近 20 个交易日的穿线次数（含今天）。≥2 = 拐头反复 = 信号差。
   * 仅拐头行有值。 */
  turn_crosses: number | null
  /** 距可交易窗口止点的剩余交易日（周内日近似）。≤15 = 交割红线，<40 = 衰减区。 */
  days_left: number | null
  /** 该月份模板的手工产业备注（DEC-069）。跟月份走不跟具体合约走，
   * JD2609/2701 与 JD2709/2801 共享「09-01」一条。没写过为 null。 */
  note: string | null
  /** 组合已到期（先到期腿的散户窗口在最新快照日前已关）。历史信号里打灰标。 */
  expired: boolean
  /** 该品种的现货与基差背景（DEC-074）。跨品种组合按第一条腿的品种给。 */
  basis: SpreadBasisInfo | null
}

/** 现货与基差。跨期两条腿相对同一个现货，所以基差之差就是价差本身——
 * 这里给的是水平与历史分位，是背景信息不是信号。 */
export interface SpreadBasisInfo {
  instrument: string
  /** 基差数据的交易日，可能早于快照日（源缺日时最多回看 7 天）。 */
  trade_date: string
  spot_price: string
  /** 主力基差 = 现货 − 主力期货。为正是期货贴水，为负是期货升水。 */
  dominant_basis: string | null
  dominant_basis_rate: string | null
  /** 基差率在该品种历年里的百分位（0~1），样本不足 60 天为 null。 */
  percentile: string | null
}

export interface SpreadMonitorResponse {
  threshold: string
  as_of: string | null
  available_dates: string[]
  items: SpreadMonitorItem[]
}

/** 某个交易日各交易所的数据到齐情况。 */
export interface DataHealthDay {
  trade_date: string
  exchanges: string[]
  /** 各所首次入库时刻(北京时间 HH:MM),键=交易所代码。2026-08-16 起装载
   * 侧不再让 upsert 刷新 loaded_at,此值即采集源当日更新时刻的画像。 */
  arrivals?: Record<string, string>
}

export interface DataHealthResponse {
  /** 近期出现过的交易所全集,界面拿它当「该有几家」的基准(不写死名单)。 */
  expected_exchanges: string[]
  /** 席位/行情各自该有几家 —— INE 只有行情没有席位,两边期望不同(2026-09-01)。
   *  老产物没有这两个字段,前端取不到时退回并集(与修复前同行为)。 */
  expected_seat_exchanges?: string[]
  expected_price_exchanges?: string[]
  seats: DataHealthDay[]
  prices: DataHealthDay[]
}

export function getDataHealth(): Promise<ApiEnvelope<DataHealthResponse>> {
  return getJson('/api/v1/overview/data-health')
}

export function getSpreadMonitor(
  threshold?: number,
  tradeDate?: string,
  history?: boolean
): Promise<ApiEnvelope<SpreadMonitorResponse>> {
  const params = new URLSearchParams()
  if (threshold !== undefined) params.set('threshold', String(threshold))
  if (tradeDate) params.set('trade_date', tradeDate)
  if (history) params.set('history', 'true')
  const query = params.toString()
  return getJson(`/api/v1/spread-analytics/monitor${query ? `?${query}` : ''}`)
}
