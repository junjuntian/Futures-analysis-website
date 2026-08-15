/**
 * 席位名的拼音首字母检索：输入 `gs` 找到「高盛」。
 *
 * 内置一张表而不是拉一个拼音库：这里要覆盖的只是期货公司名。实测生产库 328 个
 * 归一后的会员名一共用到 227 个汉字，整张表不到 2KB，比引入通用拼音包划算得多，
 * 也不用为一个搜索框多背一个依赖。
 *
 * **表里没有的字直接跳过**——那家仍然能按中文搜到，只是拼音搜不到。这是降级，
 * 不是故障。新会员带来生字时，往下面对应的那一行补一个字即可。
 *
 * 多音字放进多个组（「行」在 h 和 x、「重」在 c 和 z、「长」在 c 和 z），
 * 任一读音匹配上就算命中——与其纠结哪个读音对，不如都认。
 */
const INITIAL_GROUPS: Record<string, string> = {
  a: '安澳',
  b: '倍北宝波渤邦部',
  c: '产创城川成昌晨诚辰财长重',
  d: '东代地大天德第电达迪道都鼎沌',
  e: '尔',
  f: '丰佛发方富峰福锋风',
  g: '光公冠国工广根格港谷高钢甘莞',
  h: '华合和宏弘徽恒惠汇河海混湖红航豪货辉鸿黄黑行',
  j: '久九交京吉嘉建江津金鑫锦集际纪经疆祈君',
  k: '坤控矿科',
  l: '利力岭林理立粮联良陆陇连龙鲁',
  m: '摩民美茂贸迈马',
  n: '农南宁年牛能',
  p: '平浦蓬鹏',
  q: '乾前勤奇庆期琪齐',
  r: '瑞融',
  s: '三上世司山实石深生申盛神穗肃色苏商晟时杉首陕',
  t: '台同团塔天投特泰通铜',
  w: '万五伟吴文无物闻',
  x: '信先兴新星湘西象锡行',
  y: '一业云亚元冶友原永有易屿烟煜粤越运衍英雅阳银源',
  z: '中子州招正浙智紫证珠重长圳'
}

/** 汉字 → 可能的首字母集合。由上表反查而来。 */
const CHAR_INITIALS: Record<string, string> = {}
for (const [initial, chars] of Object.entries(INITIAL_GROUPS)) {
  for (const char of chars) {
    CHAR_INITIALS[char] = (CHAR_INITIALS[char] ?? '') + initial
  }
}

/** 这个字在表里有没有首字母。表的覆盖率测试靠它，也方便排查「为什么搜不到」。 */
export function hasInitial(char: string): boolean {
  return Boolean(CHAR_INITIALS[char])
}

/**
 * `query`（纯字母，已小写）能否匹配 `name` 中**连续若干个字**的首字母。
 *
 * 从任意位置起匹配，所以「中信期货」既能用 `zx` 也能用 `qh` 找到。要求连续是有意的：
 * 允许跳字的话，四个字的名字几乎能被任意两个字母命中，搜索框就没用了。
 */
export function pinyinHit(name: string, query: string): boolean {
  if (!query) return false
  const chars = [...name]
  for (let start = 0; start + query.length <= chars.length; start++) {
    let matched = true
    for (let offset = 0; offset < query.length; offset++) {
      const candidates = CHAR_INITIALS[chars[start + offset]]
      if (!candidates || !candidates.includes(query[offset])) {
        matched = false
        break
      }
    }
    if (matched) return true
  }
  return false
}

/**
 * 搜索框通用的匹配：中文原样包含，或纯字母时按拼音首字母。
 *
 * 大小写一律不敏感——运营者不会为了搜一下而去按 shift。
 */
export function searchHit(name: string, rawQuery: string): boolean {
  const query = rawQuery.trim().toLowerCase()
  if (!query) return true
  if (name.toLowerCase().includes(query)) return true
  // 只有纯字母的输入才走拼音：中文输入直接按上面那条包含匹配。
  return /^[a-z]+$/.test(query) && pinyinHit(name, query)
}
