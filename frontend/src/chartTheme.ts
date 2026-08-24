import { isDark } from './theme'

/**
 * ECharts 配色 token(与 styles.css 的 --tv-* 同源,双份维护)。
 *
 * ECharts 的 option 是纯 JS 对象,不认 CSS 变量,所以图表颜色在这里以常量
 * 维护两套。改 styles.css 的对应色必须同步改这里。
 *
 * 涨跌语义:红涨绿跌(国内期货惯例)。up=红,down=绿。
 *
 * 图表构建函数在渲染时调用 chartTokens() 取当前主题的值;切主题时 App.vue
 * 用 :key 重挂载视图,图表整体重建,无需各图表自行监听。
 */

export interface ChartTokens {
  /** 卡片/图表背景(图片导出用它当底色,否则深色主题导出白底不可读) */
  cardBg: string
  /** 涨/正值(红) */
  up: string
  /** 跌/负值(绿) */
  down: string
  /** 强调色(当前值标线等) */
  accent: string
  /** 蓝色主色 */
  blue: string
  /** 轴标签文字 */
  axisLabel: string
  /** 轴线 */
  axisLine: string
  /** 网格分隔线 */
  splitLine: string
  /** 零轴等实线基准 */
  baseline: string
  /** 换月等次要虚线 */
  boundary: string
  /** 图例文字 */
  legend: string
  /** tooltip 背景 */
  tooltipBg: string
  /** tooltip 文字 */
  tooltipText: string
  /** tooltip 边框 */
  tooltipBorder: string
  /** dataZoom 滑钮边框 */
  sliderBorder: string
  /** dataZoom 选区填充 */
  sliderFiller: string
  /** dataZoom 手柄 */
  sliderHandle: string
  /** dataZoom 背景线 */
  sliderDataLine: string
  /** dataZoom 背景面 */
  sliderDataArea: string
  /** 叠年图当前年颜色 */
  currentYear: string
  /** 叠年图当前年结尾标签 */
  currentYearLabel: string
  /** 叠年图历史年份色板 */
  seasonalPalette: string[]
}

const LIGHT: ChartTokens = {
  cardBg: '#ffffff',
  up: '#f23645',
  down: '#089981',
  accent: '#f57c00',
  blue: '#2962ff',
  axisLabel: '#6a6d78',
  axisLine: '#d1d4dc',
  splitLine: '#f0f3fa',
  baseline: '#b2b5be',
  boundary: '#d1d4dc',
  legend: '#6a6d78',
  tooltipBg: '#ffffff',
  tooltipText: '#131722',
  tooltipBorder: '#e0e3eb',
  sliderBorder: '#e0e3eb',
  sliderFiller: 'rgba(41, 98, 255, 0.10)',
  sliderHandle: '#b2b5be',
  sliderDataLine: '#d1d4dc',
  sliderDataArea: '#f0f3fa',
  currentYear: '#2962ff',
  currentYearLabel: '#1e53e5',
  seasonalPalette: [
    '#f23645', '#089981', '#9a4fb5', '#c79a1e', '#1f9a91',
    '#b8437b', '#5c6ac4', '#7d8b2c', '#a2563a', '#3a8fd4',
    '#8a5cd6', '#2d9a5b'
  ]
}

const DARK: ChartTokens = {
  cardBg: '#1e222d',
  up: '#f7525f',
  down: '#22ab94',
  accent: '#ffa726',
  blue: '#5b83f7',
  axisLabel: '#9598a1',
  axisLine: '#363a45',
  splitLine: '#1e222d',
  baseline: '#50535e',
  boundary: '#363a45',
  legend: '#9598a1',
  tooltipBg: '#1e222d',
  tooltipText: '#d1d4dc',
  tooltipBorder: '#363a45',
  sliderBorder: '#2a2e39',
  sliderFiller: 'rgba(41, 98, 255, 0.16)',
  sliderHandle: '#50535e',
  sliderDataLine: '#363a45',
  sliderDataArea: '#262b38',
  currentYear: '#5b83f7',
  currentYearLabel: '#86a3f9',
  seasonalPalette: [
    '#f7525f', '#22ab94', '#b085d6', '#d4ac3a', '#3cb5ab',
    '#d66a94', '#7986cb', '#9aab4a', '#c47a5c', '#5ba6e8',
    '#a687e0', '#4cb578'
  ]
}

export function chartTokens(): ChartTokens {
  return isDark() ? DARK : LIGHT
}

/** ECharts tooltip 的统一样式片段(各图表展开进 tooltip 配置)。 */
export function tooltipStyle() {
  const tokens = chartTokens()
  return {
    // 挂到 body 上(DEC-132 加逐家明细后小窗变高):原先小窗是卡片的子元素,超出卡片
    // 底边的部分被 overflow 裁掉,K 线图上逐家那几行看不见(运营者 2026-08-24 报)。
    appendToBody: true,
    // 自己定位,不用 confine:confine 把小窗按进图表区,而逐家明细让小窗**比图表区还高**,
    // 按进去必然裁尾。规则:横向贴鼠标、越界翻到另一侧;纵向居中、贴不下就贴容器底、
    // 还不够就向上探出容器顶 —— appendToBody 后探出的部分照样完整可见。
    confine: false,
    position(
      point: [number, number],
      _params: unknown,
      _dom: unknown,
      _rect: unknown,
      size: { contentSize: [number, number]; viewSize: [number, number] }
    ) {
      const [w, h] = size.contentSize
      const [vw, vh] = size.viewSize
      let x = point[0] + 18
      if (x + w > vw) x = Math.max(0, point[0] - w - 18)
      let y = point[1] - h / 2
      if (y + h > vh) y = vh - h
      return [x, y]
    },
    backgroundColor: tokens.tooltipBg,
    borderColor: tokens.tooltipBorder,
    textStyle: { color: tokens.tooltipText }
  }
}

/** dataZoom slider 的统一样式片段。 */
export function sliderStyle() {
  const tokens = chartTokens()
  return {
    borderColor: tokens.sliderBorder,
    fillerColor: tokens.sliderFiller,
    handleStyle: { color: tokens.sliderHandle },
    dataBackground: {
      lineStyle: { color: tokens.sliderDataLine },
      areaStyle: { color: tokens.sliderDataArea }
    }
  }
}
