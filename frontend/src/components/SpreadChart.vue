<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { BarChart, CandlestickChart, LineChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent
} from 'echarts/components'
import { connect, init, use, type EChartsType } from 'echarts/core'
import { CanvasRenderer, SVGRenderer } from 'echarts/renderers'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { chartTokens } from '../chartTheme'
import { sanitizeSvgDataUrl } from '../spreadCharts'

const props = withDefaults(defineProps<{
  option: EChartsOption
  height?: number
  exportName?: string
  /**
   * 联动组。同组的图共享缩放与悬停位置——拖上面那张的滑钮，下面那张跟着走。
   *
   * 竖着叠的几张图讲的是同一段时间，各滑各的就没法把行情和持仓对着看。
   */
  group?: string
}>(), {
  height: 360,
  exportName: 'spread-chart',
  group: undefined
})

const root = ref<HTMLDivElement>()
// Components must be registered explicitly in the tree-shaken build: an option
// for an unregistered component is silently ignored, which is why the trend
// chart rendered without its zoom slider.
// tree-shaken 构建里没注册的类型会静默不画——DataZoom 就这么坏过一次，
// 图上什么都没有而控制台一声不响。建仓过程要 K 线和柱状图。
use([
  BarChart,
  CandlestickChart,
  LineChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
  SVGRenderer
])

let chart: EChartsType | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart) return
  chart.setOption(props.option, { notMerge: true })
}

function download(type: 'png' | 'svg') {
  if (!chart) return
  let raw: string
  if (type === 'png') {
    const holder = document.createElement('div')
    holder.style.cssText = `position:fixed;left:-10000px;top:0;width:${chart.getWidth()}px;height:${chart.getHeight()}px`
    document.body.appendChild(holder)
    const canvasChart = init(holder, undefined, { renderer: 'canvas' })
    try {
      canvasChart.setOption(props.option, { notMerge: true })
      raw = canvasChart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: chartTokens().cardBg })
    } finally {
      canvasChart.dispose()
      holder.remove()
    }
  } else {
    raw = chart.getDataURL({ type: 'svg', backgroundColor: chartTokens().cardBg })
  }
  const href = type === 'svg' ? sanitizeSvgDataUrl(raw) : raw
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = `${props.exportName}.${type}`
  anchor.rel = 'noopener'
  anchor.click()
}

defineExpose({ download })

onMounted(async () => {
  await nextTick()
  if (!root.value) return
  chart = init(root.value, undefined, { renderer: 'svg' })
  if (props.group) {
    chart.group = props.group
    // connect 认的是 group 名，重复调用无害：后挂载的图会并进已有的组。
    connect(props.group)
  }
  render()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(root.value)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div ref="root" class="spread-chart" :style="{ height: `${height}px` }" />
</template>
