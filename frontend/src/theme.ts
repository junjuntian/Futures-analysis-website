import { ref } from 'vue'

/**
 * 双主题切换(2026-08-16 TradingView 风格改造)。
 *
 * - 深色 = html.dark 类(Element Plus 官方约定,EP dark css-vars 靠它生效)。
 * - 持久化进 localStorage;index.html 里的内联脚本在 app 挂载前就读同一个键,
 *   避免深色用户刷新时闪一下白屏。
 * - App.vue 给 <router-view> 绑了 :key="mode",切主题强制重挂载当前视图——
 *   ECharts 不认 CSS 变量,重挂载让所有图表带着新配色重建,省掉每个视图各写
 *   一遍 watch。单人面板,重拉一次数据的代价可以接受。
 */

const STORAGE_KEY = 'fap-theme'

export type ThemeMode = 'light' | 'dark'

function initialMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* Safari 隐私模式等场景 localStorage 会抛,当无偏好处理 */ }
  return 'dark'
}

export const themeMode = ref<ThemeMode>(initialMode())

export function applyTheme(mode: ThemeMode): void {
  themeMode.value = mode
  document.documentElement.classList.toggle('dark', mode === 'dark')
  try {
    localStorage.setItem(STORAGE_KEY, mode)
  } catch { /* 同上 */ }
}

export function toggleTheme(): void {
  applyTheme(themeMode.value === 'dark' ? 'light' : 'dark')
}

export function isDark(): boolean {
  return themeMode.value === 'dark'
}
