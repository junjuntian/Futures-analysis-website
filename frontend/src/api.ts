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
  days: BuildingDay[]
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
}

export interface DataHealthResponse {
  /** 近期出现过的交易所全集,界面拿它当「该有几家」的基准(不写死名单)。 */
  expected_exchanges: string[]
  seats: DataHealthDay[]
  prices: DataHealthDay[]
}

export function getDataHealth(): Promise<ApiEnvelope<DataHealthResponse>> {
  return getJson('/api/v1/overview/data-health')
}

export function getSpreadMonitor(
  threshold?: number,
  tradeDate?: string
): Promise<ApiEnvelope<SpreadMonitorResponse>> {
  const params = new URLSearchParams()
  if (threshold !== undefined) params.set('threshold', String(threshold))
  if (tradeDate) params.set('trade_date', tradeDate)
  const query = params.toString()
  return getJson(`/api/v1/spread-analytics/monitor${query ? `?${query}` : ''}`)
}
