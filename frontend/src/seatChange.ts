// 席位增减量的求和规则。
//
// 提成独立模块只为一件事：能被测试直接调到。放在 SeatsView.vue 的 script setup 里
// 是测不到的，而这段逻辑刚刚出过一次线上错误——不是崩溃，是**安静地显示了错的数字**。
//
// 三种状态必须分清：
//   undefined  这一侧还没有任何行（某合约只上了空头榜，多头侧就是这个状态）
//   null       有行，但数据源没给增减量（上期所 akshare_v1 的席位数据集无 change 字段）
//   number     真实的增减量
//
// 2026-08-11 上期所席位当天只有 akshare_v1 一个源，change 全为 NULL，而页面写的是
// `(acc ?? 0) + (delta ?? 0)`——未知被当成 0 加进去，整页每格显示 0，看上去像
// 「所有席位一整天一手没动」。真值是高盛 AU2610 减 31 手。
// **把「不知道」渲染成「没变化」是最坏的一种错：它不像缺数据，像真数据。**

/// 未知具有吸收性：任一分量是 null，和就是 null。
/// undefined 表示尚无分量，遇到第一个真值就直接采用它。
export function addChange(
  acc: number | null | undefined,
  delta: number | null
): number | null | undefined {
  if (acc === null || delta === null) return null
  return (acc ?? 0) + delta
}

/// 一侧没有任何行时按 0 计——「该合约没上多头榜」不等于「多头变化不可知」。
/// 只有 null（有行而源没给）才继续保持未知。
export function sideDelta(value: number | null | undefined): number | null {
  return value === undefined ? 0 : value
}

/// 显示用：未知与无行都不印数字。空着比印一个 0 诚实。
export function signedChange(value: number | null | undefined): string {
  if (value === null || value === undefined) return ''
  return value > 0 ? `+${value}` : String(value)
}
