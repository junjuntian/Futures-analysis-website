import { describe, expect, it } from 'vitest'
import type { ContinuousSpreadPoint } from './api'
import { buildSignedLineSegments, sanitizeSvgDataUrl } from './spreadCharts'

function point(index: number, value: number, segmentNo = 1): ContinuousSpreadPoint {
  return {
    trade_date: `2025-01-${String(index + 2).padStart(2, '0')}`,
    value,
    from_code: segmentNo === 1 ? 'jm2509' : 'jm2609',
    to_code: segmentNo === 1 ? 'jm2601' : 'jm2701',
    segment_no: segmentNo
  }
}

describe('free spread chart contracts', () => {
  it('linearly inserts an exact zero and splits red/green segments', () => {
    const segments = buildSignedLineSegments([point(0, 6), point(1, -2)])
    expect(segments).toHaveLength(2)
    expect(segments[0]).toEqual({
      sign: 'positive',
      sourcePair: 'jm2509-jm2601',
      points: [[0, 6], [0.75, 0]]
    })
    expect(segments[1]).toEqual({
      sign: 'negative',
      sourcePair: 'jm2509-jm2601',
      points: [[0.75, 0], [1, -2]]
    })
  })

  it('never connects two actual-contract segments', () => {
    const segments = buildSignedLineSegments([point(0, 2, 1), point(1, 3, 2)])
    expect(segments).toHaveLength(2)
    expect(segments[0].points).toEqual([[0, 2]])
    expect(segments[1].points).toEqual([[1, 3]])
  })

  it('keeps one curve when the server splits a segment without changing the pair', () => {
    // The upstream alternates the forward and reverse pair day by day around
    // each January expiry, so the same leg pair arrives as several segments
    // once the reverse points are excluded. That is not a contract roll.
    const split = [point(0, 2, 1), { ...point(1, 3, 1), segment_no: 7 }]
    const segments = buildSignedLineSegments(split)
    expect(segments).toHaveLength(1)
    expect(segments[0].points).toEqual([[0, 2], [1, 3]])
  })

  it('sanitizes script, event handlers, and external links from SVG export', () => {
    const unsafe = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
      + '<style>@import url(https://evil.example/a.css)</style>'
      + '<a href="https://evil.example" onclick="alert(2)" style="fill:url(https://evil.example/a)"><text>chart</text></a>'
      + '<use href="#safe"/></svg>'
    const cleanedUrl = sanitizeSvgDataUrl(`data:image/svg+xml;charset=UTF-8,${encodeURIComponent(unsafe)}`)
    const cleaned = decodeURIComponent(cleanedUrl.slice(cleanedUrl.indexOf(',') + 1))
    expect(cleaned).not.toContain('<script')
    expect(cleaned).not.toContain('onclick')
    expect(cleaned).not.toContain('evil.example')
    expect(cleaned).not.toContain('@import')
    expect(cleaned).toContain('href="#safe"')
  })
})
