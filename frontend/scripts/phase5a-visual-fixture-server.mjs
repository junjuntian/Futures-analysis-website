import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(fileURLToPath(new URL('..', import.meta.url)), 'dist')
const source = {
  provider: 'sanhe', source_code: 'sanhe_spread_readonly', source_display_name: '三禾数据',
  source_type: 'aggregator', fetched_at: '2026-08-05T08:08:00Z', data_cutoff_at: '2026-08-04',
  price_basis: 'upstream_spread', raw_leg_prices_available: false
}
const varieties = [
  { market: '大商所', name: '焦煤', symbol: 'JM' },
  { market: '郑商所', name: '玻璃', symbol: 'FG' },
  { market: '郑商所', name: '纯碱', symbol: 'SA' },
  { market: '上期所', name: '热轧卷板', symbol: 'HC' },
  { market: '上期所', name: '螺纹钢', symbol: 'RB' },
  { market: '大商所', name: '豆油', symbol: 'Y' },
  { market: '大商所', name: '棕榈油', symbol: 'P' },
  { market: '大商所', name: '豆粕', symbol: 'M' }
]

const pointValues = [42, 65, 28, 83, 102, 74, 91, 118, -24, -48, -29, -67, -92, -61, 15, 57, 86, 104, 82, 116]
const continuousPoints = pointValues.map((value, index) => {
  const year = 2024 + Math.floor(index / 8)
  const month = ((index * 2) % 8) + 1
  const day = 2 + ((index * 3) % 23)
  return {
    trade_date: `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
    value,
    from_code: year === 2024 ? 'jm2409' : year === 2025 ? 'jm2509' : 'jm2609',
    to_code: year === 2024 ? 'jm2501' : year === 2025 ? 'jm2601' : 'jm2701',
    segment_no: year - 2023
  }
}).sort((left, right) => left.trade_date.localeCompare(right.trade_date))
const seasonalAxis = ['01-20', '02-20', '03-20', '04-20', '05-20', '06-20', '07-20', '08-31']
const trace = {
  provider: 'sanhe', source_code: source.source_code, data_cutoff_at: source.data_cutoff_at,
  price_basis: 'upstream_spread', sample_start: continuousPoints[0].trade_date,
  sample_end: continuousPoints.at(-1).trade_date, sample_count: continuousPoints.length,
  excluded_point_count: 6, calendar_version_ids: ['019c2ad8-e000-7000-8000-000000000101'],
  window_algorithm_version: 'retail_window_v1', statistics_algorithm_version: 'spread_window_stats_v1',
  rule_version: 'retail-window-default-v1'
}
const monthlyRows = [2024, 2025, 2026].map((year, row) => ({
  year,
  months: Array.from({ length: 12 }, (_, index) => ({
    month: index + 1,
    delta: index < 8 ? [63, -15, 48, 17, -115, -46, -18, 32][(index + row * 2) % 8] : null,
    sample_count: index < 8 ? 18 : 0,
    is_partial: year === 2026 && index === 7
  }))
}))
const spreadResult = {
  series_id: '019c2ad8-e000-7000-8000-000000000201', source,
  query: { provider: 'sanhe', leg1: { variety: '焦煤', symbol: 'JM', month: '09' },
    leg2: { variety: '焦煤', symbol: 'JM', month: '01' } },
  quality: { status: 'ok', input_point_count: continuousPoints.length + 6,
    retained_point_count: continuousPoints.length, excluded_point_count: 6, missing_contract_point_count: 0 },
  algorithm_versions: { provider: 'sanhe_spread_v1', window: 'retail_window_v1',
    statistics: 'spread_window_stats_v1', rule: 'retail-window-default-v1' },
  continuous_series: {
    trace, points: continuousPoints,
    segment_boundaries: [
      { segment_no: 1, trade_date: continuousPoints[0].trade_date, from_code: 'jm2409', to_code: 'jm2501', reason: 'series_start' },
      { segment_no: 2, trade_date: continuousPoints.find((point) => point.segment_no === 2).trade_date,
        from_code: 'jm2509', to_code: 'jm2601', previous_from_code: 'jm2409', previous_to_code: 'jm2501', reason: 'contract_roll' },
      { segment_no: 3, trade_date: continuousPoints.find((point) => point.segment_no === 3).trade_date,
        from_code: 'jm2609', to_code: 'jm2701', previous_from_code: 'jm2509', previous_to_code: 'jm2601', reason: 'contract_roll' }
    ],
    current_value: continuousPoints.at(-1).value
  },
  seasonal_series: {
    trace, axis: seasonalAxis, current_year: 2026,
    years: [
      { year: 2024, values: [72, 64, 59, 81, 74, 66, 88, 83], sample_count: 8, sample_start: '2024-01-20', sample_end: '2024-08-31' },
      { year: 2025, values: [48, 55, 61, 52, 46, 58, 51, 44], sample_count: 8, sample_start: '2025-01-20', sample_end: '2025-08-31' },
      { year: 2026, values: [39, 51, 68, 82, 97, 105, 112, 118], sample_count: 8, sample_start: '2026-01-20', sample_end: '2026-08-31' }
    ]
  },
  monthly_matrix: {
    trace, years: monthlyRows,
    up_ratios: Array.from({ length: 12 }, (_, index) => ({ month: index + 1,
      ratio: index < 8 ? [0.45, 0.33, 0.58, 0.62, 0.54, 0.46, 0.38, 0.5][index] : null,
      positive_year_count: index < 8 ? 1 : 0, eligible_year_count: index < 8 ? 3 : 0 }))
  },
  segments: []
}

function envelope(data) {
  return JSON.stringify({ data, meta: { request_id: '019c2ad8-e000-7000-8000-000000000301' } })
}

function apiResponse(request, response) {
  const url = new URL(request.url, 'http://127.0.0.1')
  let data
  if (url.pathname === '/api/v1/auth/me') data = { user: { id: 'user-1', username: 'visual-fixture', roles: ['analyst'], permissions: ['read_spreads'] }, workspace: { id: 'workspace-1', name: 'Visual QA' } }
  else if (url.pathname === '/api/v1/workspace') data = { id: 'workspace-1', name: 'Visual QA' }
  else if (url.pathname === '/api/v1/auth/csrf') data = { csrf_token: 'visual-fixture-csrf' }
  else if (url.pathname.endsWith('/providers/sanhe/varieties')) data = { source, items: varieties, result_kind: 'ok' }
  else if (url.pathname.includes('/providers/sanhe/varieties/') && url.pathname.endsWith('/months')) data = { source, variety: '焦煤', months: ['01', '05', '09'], basis: 1, basis_semantics_confirmed: false, result_kind: 'ok' }
  else if (url.pathname.endsWith('/favorites')) data = []
  else if (url.pathname.endsWith('/free-spread/query')) data = spreadResult
  else return false
  response.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  response.end(envelope(data))
  return true
}

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' }
createServer((request, response) => {
  if (request.url.startsWith('/api/')) {
    if (!apiResponse(request, response)) {
      response.writeHead(404).end()
    }
    return
  }
  const rawPath = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname)
  const relative = normalize(rawPath).replace(/^(\.\.[/\\])+/, '').replace(/^[/\\]+/, '')
  let path = join(root, relative)
  if (!existsSync(path) || statSync(path).isDirectory()) path = join(root, 'index.html')
  response.writeHead(200, { 'content-type': mime[extname(path)] ?? 'application/octet-stream', 'cache-control': 'no-store' })
  createReadStream(path).pipe(response)
}).listen(4173, '127.0.0.1', () => console.log('phase5a visual fixture http://127.0.0.1:4173'))
