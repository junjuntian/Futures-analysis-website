import type { EChartsOption, SeriesOption } from 'echarts'
import type { FreeSpreadQueryResponse } from './api'
// 颜色全部走 chartTheme token(双主题)。正价差=红、负价差=绿,沿用红涨绿跌
// 语义;叠年图历史年份用区分度色板,当前年单独高亮,理由见 chartTheme.ts。
import { chartTokens, sliderStyle, tooltipStyle } from './chartTheme'

/**
 * 最近一年的起点下标。带时间轴的图默认落在这一段。
 *
 * 十八年的日线挤在一屏里读不出任何东西，而运营者每次进来都得手动拉一次滑钮。
 * 按**日历一年**往回找，不按「多少个交易日」——交易日数逐年不同（春节长短、
 * 停市），按天数才叫一年。
 *
 * @param dates 升序的交易日串（`YYYY-MM-DD`）。字符串直接比大小即可，这个格式
 *   的字典序就是时间序，不必转 Date 再比。
 */
export function lastYearStartIndex(dates: string[]): number {
  if (dates.length === 0) return 0
  // 拼 `T00:00:00` 让它按本地时区解析。少了它某些浏览器按 UTC 解析，东八区会差一天。
  const cutoff = new Date(`${dates[dates.length - 1]}T00:00:00`)
  cutoff.setFullYear(cutoff.getFullYear() - 1)
  const pad = (value: number) => String(value).padStart(2, '0')
  const key = `${cutoff.getFullYear()}-${pad(cutoff.getMonth() + 1)}-${pad(cutoff.getDate())}`
  const index = dates.findIndex((date) => date >= key)
  return index < 0 ? 0 : index
}

export function continuousChartOption(data: FreeSpreadQueryResponse): EChartsOption {
  const tokens = chartTokens()
  const points = data.continuous_series.points
  // The upstream series alternates the forward and the reverse leg pair day by
  // day around each January expiry, so one contract roll arrives as a dozen
  // one-point segments and the server emits a boundary for every one of them.
  // Drawing them all stacks a dozen dashed lines onto neighbouring trading days
  // and paints what looks like a solid grey band. Only the boundaries whose leg
  // pair actually changed are real rolls.
  const boundaries = data.continuous_series.segment_boundaries
    .slice(1)
    .filter((boundary) => boundary.previous_from_code !== boundary.from_code
      || boundary.previous_to_code !== boundary.to_code)
  const current = data.continuous_series.current_value
  const signedSegments = buildSignedLineSegments(points)
  const boundaryIndexes = new Map(points.map((point, index) => [point.trade_date, index]))
  const markLine = {
    silent: false,
    symbol: 'none' as const,
    label: { color: tokens.axisLabel, formatter: '{b}' },
    data: [
      { name: '', yAxis: 0, lineStyle: { color: tokens.baseline, type: 'solid' as const, width: 1 } },
      ...(current === null || current === undefined ? [] : [{
        name: formatNumber(current),
        yAxis: current,
        lineStyle: { color: tokens.accent, type: 'dashed' as const, width: 1.5 },
        label: { color: tokens.accent, formatter: formatNumber(current), position: 'insideEndTop' as const }
      }]),
      ...boundaries.flatMap((boundary) => {
        const index = boundaryIndexes.get(boundary.trade_date)
        return index === undefined ? [] : [{
          name: `${boundary.previous_from_code?.toUpperCase() ?? '—'}−${boundary.previous_to_code?.toUpperCase() ?? '—'} → ${boundary.from_code.toUpperCase()}−${boundary.to_code.toUpperCase()}`,
          xAxis: index,
          lineStyle: { color: tokens.boundary, type: 'dashed' as const, width: 1 },
          label: { show: false }
        }]
      })
    ]
  }
  return {
    animation: false,
    grid: { left: 54, right: 58, top: 28, bottom: 82 },
    dataZoom: [
      // 默认落在最近一年，见 lastYearStartIndex。
      {
        type: 'inside',
        filterMode: 'none',
        startValue: lastYearStartIndex(points.map((point) => point.trade_date)),
        endValue: Math.max(points.length - 1, 0)
      },
      {
        type: 'slider',
        startValue: lastYearStartIndex(points.map((point) => point.trade_date)),
        endValue: Math.max(points.length - 1, 0),
        height: 26,
        bottom: 30,
        ...sliderStyle(),
        labelFormatter: (value: number) => points[Math.round(value)]?.trade_date ?? ''
      }
    ],
    tooltip: {
      trigger: 'axis',
      ...tooltipStyle(),
      formatter: (params: unknown) => {
        const item = Array.isArray(params) ? params[0] as { axisValue?: number } : undefined
        const point = item?.axisValue === undefined ? undefined : points[Math.round(item.axisValue)]
        return point
          ? `${point.trade_date}<br/>价差 ${formatNumber(point.value)}<br/>${point.from_code.toUpperCase()} − ${point.to_code.toUpperCase()}`
          : ''
      }
    },
    xAxis: {
      type: 'value',
      min: 0,
      max: Math.max(points.length - 1, 1),
      minInterval: 1,
      axisLabel: {
        color: tokens.axisLabel,
        hideOverlap: true,
        formatter: (value: number) => points[Math.round(value)]?.trade_date ?? ''
      },
      axisLine: { lineStyle: { color: tokens.axisLine } },
      axisTick: { show: false },
      // value 型 x 轴的 splitLine 默认开且用 ECharts 自带浅灰——深色底上比数据线
      // 还亮,喧宾夺主(2026-08-16 视觉审查抓到)。收进主题网格色。
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series: signedSegments.map((segment, index) => ({
      name: segment.sign === 'positive' ? '正价差' : '负价差',
      type: 'line',
      showSymbol: false,
      data: segment.points,
      lineStyle: { width: 2.5, color: segment.sign === 'positive' ? tokens.up : tokens.down },
      itemStyle: { color: segment.sign === 'positive' ? tokens.up : tokens.down },
      markLine: index === 0 ? markLine : undefined
    } as SeriesOption))
  }
}

export interface SignedLineSegment {
  sign: 'positive' | 'negative'
  /** `${from_code}-${to_code}` of the leg pair this run belongs to. */
  sourcePair: string
  points: Array<[number, number]>
}

/**
 * Builds a trading-day index axis, breaks every contract roll, and inserts a
 * linearly interpolated zero at each sign crossing. This prevents both a fake
 * roll connection and a red/green gradient approximation around zero.
 *
 * A roll is a change of leg pair, not a change of `segment_no`. The server
 * starts a new segment whenever the upstream run of identical codes ends, and
 * around each January expiry the upstream alternates the forward and reverse
 * pair day by day, so the same pair spans several segments. Breaking on
 * `segment_no` there would chop the curve into single-point fragments even
 * though no contract changed.
 */
export function buildSignedLineSegments(
  points: FreeSpreadQueryResponse['continuous_series']['points']
): SignedLineSegment[] {
  const output: SignedLineSegment[] = []
  let active: SignedLineSegment | undefined
  const begin = (sign: SignedLineSegment['sign'], sourcePair: string, point: [number, number]) => {
    active = { sign, sourcePair, points: [point] }
    output.push(active)
  }
  const pairOf = (point: FreeSpreadQueryResponse['continuous_series']['points'][number]) =>
    `${point.from_code}-${point.to_code}`
  points.forEach((point, index) => {
    const sign = point.value >= 0 ? 'positive' : 'negative'
    const coordinate: [number, number] = [index, point.value]
    const previous = points[index - 1]
    const pair = pairOf(point)
    if (!previous || pairOf(previous) !== pair) {
      begin(sign, pair, coordinate)
      if (point.value === 0) active = undefined
      return
    }
    if (point.value === 0) {
      if (active) active.points.push(coordinate)
      else begin('positive', pair, coordinate)
      active = undefined
      return
    }
    if (previous.value === 0) {
      begin(sign, pair, [index - 1, 0])
      active?.points.push(coordinate)
      return
    }
    const previousSign = previous.value >= 0 ? 'positive' : 'negative'
    if (previousSign === sign) {
      if (!active || active.sourcePair !== pair || active.sign !== sign) {
        begin(sign, pair, [index - 1, previous.value])
      }
      active?.points.push(coordinate)
      return
    }
    const zeroIndex = (index - 1) + Math.abs(previous.value) / (Math.abs(previous.value) + Math.abs(point.value))
    active?.points.push([zeroIndex, 0])
    begin(sign, pair, [zeroIndex, 0])
    active?.points.push(coordinate)
  })
  return output
}

export function seasonalChartOption(data: FreeSpreadQueryResponse): EChartsOption {
  const tokens = chartTokens()
  const currentYear = data.seasonal_series.current_year
  // The axis spans every calendar day of the window, so days no year traded
  // (weekends, holidays, dates outside every year's tradable window) leave
  // holes that break each line into fragments. Keep only the slots at least
  // one year traded and connect the remainder, which yields one continuous
  // curve per year over just the tradable days.
  const keptSlots = data.seasonal_series.axis
    .map((_, index) => index)
    .filter((index) => data.seasonal_series.years.some((year) => year.values[index] !== null
      && year.values[index] !== undefined))
  const axis = keptSlots.map((index) => data.seasonal_series.axis[index])
  let paletteCursor = 0
  const series: SeriesOption[] = data.seasonal_series.years.map((year) => {
    const isCurrent = year.year === currentYear
    const color = isCurrent
      ? tokens.currentYear
      : tokens.seasonalPalette[paletteCursor++ % tokens.seasonalPalette.length]
    return {
      name: String(year.year),
      type: 'line',
      showSymbol: false,
      connectNulls: true,
      data: keptSlots.map((index) => year.values[index] ?? null),
      lineStyle: { color, width: isCurrent ? 3.5 : 1.6, opacity: isCurrent ? 1 : 0.85 },
      itemStyle: { color },
      emphasis: { focus: 'series', lineStyle: { width: 3.2, opacity: 1 } },
      endLabel: isCurrent
        ? { show: true, formatter: String(year.year), color: tokens.currentYearLabel, fontSize: 15 }
        : { show: false }
    } as SeriesOption
  })
  return {
    animation: false,
    // bottom 要留够滑钮的位置，否则滑钮压在横轴标签上。
    grid: { left: 54, right: 76, top: 74, bottom: 76 },
    // 与走势图同一套滑钮。叠年图横轴是日历日，想比某一段（比如换月前后两周）
    // 各年的走势，没有滑钮只能眯着眼在一整年里找。
    dataZoom: [
      // 滚轮留给页面。缩放交给滑钮，图内按住拖动仍可平移。
      { type: 'inside', zoomOnMouseWheel: false, moveOnMouseWheel: false },
      {
        type: 'slider',
        height: 26,
        bottom: 24,
        ...sliderStyle(),
        labelFormatter: (value: number) => axis[Math.round(value)]?.replace('-', '/') ?? ''
      }
    ],
    legend: {
      top: 8,
      left: 0,
      type: 'scroll',
      textStyle: { color: tokens.legend, fontSize: 13 },
      selected: Object.fromEntries(data.seasonal_series.years.map((year, index, all) => [
        String(year.year),
        year.year === currentYear || index >= all.length - 3
      ]))
    },
    tooltip: { trigger: 'axis', ...tooltipStyle() },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: axis.map((value) => value.replace('-', '/')),
      axisLabel: { color: tokens.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: tokens.axisLine } },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: tokens.axisLabel },
      splitLine: { lineStyle: { color: tokens.splitLine } }
    },
    series
  }
}

export function sanitizeSvgDataUrl(dataUrl: string): string {
  if (!dataUrl.startsWith('data:image/svg+xml')) {
    throw new Error('chart export did not return SVG')
  }
  const comma = dataUrl.indexOf(',')
  if (comma < 0) throw new Error('invalid SVG data URL')
  const header = dataUrl.slice(0, comma)
  const encoded = dataUrl.slice(comma + 1)
  const markup = header.includes(';base64') ? atob(encoded) : decodeURIComponent(encoded)
  const documentNode = new DOMParser().parseFromString(markup, 'image/svg+xml')
  if (documentNode.querySelector('parsererror')) throw new Error('invalid SVG document')
  documentNode.querySelectorAll('script, foreignObject, iframe, object, embed, image, audio, video, style, link')
    .forEach((node) => node.remove())
  documentNode.querySelectorAll('*').forEach((node) => {
    for (const attribute of Array.from(node.attributes)) {
      const name = attribute.name.toLowerCase()
      const value = attribute.value.trim().toLowerCase()
      const isReference = name === 'href' || name.endsWith(':href') || name === 'src'
      const hasDangerousStyle = name === 'style'
        && (value.includes('javascript:') || value.includes('expression(')
          || value.includes('@import') || /url\(\s*["']?(?!#)/.test(value))
      const hasExternalUrlFunction = ['fill', 'stroke', 'filter', 'clip-path', 'mask', 'cursor']
        .includes(name) && /url\(\s*["']?(?!#)/.test(value)
      if (name.startsWith('on') || (isReference && !value.startsWith('#'))
        || hasDangerousStyle || hasExternalUrlFunction) {
        node.removeAttribute(attribute.name)
      }
    }
  })
  const cleaned = new XMLSerializer().serializeToString(documentNode.documentElement)
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(cleaned)}`
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)
}
