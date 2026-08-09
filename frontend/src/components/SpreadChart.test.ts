import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import SpreadChart from './SpreadChart.vue'

const charts = vi.hoisted(() => {
  const make = (dataUrl: string) => ({
    setOption: vi.fn(),
    getDataURL: vi.fn(() => dataUrl),
    getWidth: vi.fn(() => 800),
    getHeight: vi.fn(() => 360),
    resize: vi.fn(),
    dispose: vi.fn()
  })
  return {
    svg: make('data:image/svg+xml;charset=UTF-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Ctext%3Echart%3C%2Ftext%3E%3C%2Fsvg%3E'),
    canvas: make('data:image/png;base64,cG5n')
  }
})

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn((_element: HTMLElement, _theme: unknown, options: { renderer: string }) =>
    options.renderer === 'canvas' ? charts.canvas : charts.svg)
}))
vi.mock('echarts/charts', () => ({ LineChart: {} }))
vi.mock('echarts/components', () => ({
  DataZoomComponent: {}, GridComponent: {}, LegendComponent: {},
  MarkLineComponent: {}, TooltipComponent: {}
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {}, SVGRenderer: {} }))

class ResizeObserverStub {
  observe = vi.fn()
  disconnect = vi.fn()
}

describe('SpreadChart export', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    vi.clearAllMocks()
  })

  it('downloads PNG through an isolated canvas renderer and disposes it', async () => {
    const wrapper = mount(SpreadChart, {
      props: { option: { series: [] }, exportName: '价差走势' }
    })
    await nextTick()
    ;(wrapper.vm as unknown as { download: (type: 'png') => void }).download('png')

    expect(charts.canvas.setOption).toHaveBeenCalledOnce()
    expect(charts.canvas.getDataURL).toHaveBeenCalledWith({
      type: 'png', pixelRatio: 2, backgroundColor: '#fff'
    })
    expect(charts.canvas.dispose).toHaveBeenCalledOnce()
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce()
    await wrapper.unmount()
  })

  it('sanitizes and downloads SVG from the visible SVG renderer', async () => {
    const wrapper = mount(SpreadChart, {
      props: { option: { series: [] }, exportName: '季节叠年' }
    })
    await nextTick()
    ;(wrapper.vm as unknown as { download: (type: 'svg') => void }).download('svg')

    expect(charts.svg.getDataURL).toHaveBeenCalledWith({ type: 'svg', backgroundColor: '#fff' })
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce()
    await wrapper.unmount()
  })
})
