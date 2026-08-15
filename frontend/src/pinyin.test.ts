import { describe, expect, it } from 'vitest'
import { hasInitial, pinyinHit, searchHit } from './pinyin'

describe('拼音首字母检索', () => {
  it('两个首字母能找到那家席位', () => {
    expect(pinyinHit('高盛', 'gs')).toBe(true)
    expect(pinyinHit('中信期货', 'zx')).toBe(true)
    expect(pinyinHit('永安期货', 'ya')).toBe(true)
    expect(pinyinHit('国泰君安', 'gt')).toBe(true)
  })

  it('从名字中间起也能匹配', () => {
    // 「中信期货」既能用 zx 也能用 qh 找到——他记得住哪半截就用哪半截。
    expect(pinyinHit('中信期货', 'qh')).toBe(true)
    expect(pinyinHit('国泰君安', 'ja')).toBe(true)
  })

  it('要求连续，不允许跳字', () => {
    // 允许跳字的话，四个字的名字几乎能被任意两个字母命中，搜索框就没用了。
    // 「中信期货」= z-x-q-h，「zq」跳过了「信」，不该命中。
    expect(pinyinHit('中信期货', 'zq')).toBe(false)
    expect(pinyinHit('国泰君安', 'ga')).toBe(false)
  })

  it('多音字的两个读音都认', () => {
    // 重庆 chóng、长江 cháng；「重」「长」也各有 z 的读音。与其纠结哪个对，不如都认。
    expect(pinyinHit('重庆期货', 'cq')).toBe(true)
    expect(pinyinHit('长江期货', 'cj')).toBe(true)
    expect(pinyinHit('长江期货', 'zj')).toBe(true)
  })

  it('表里没有的字不命中，也不抛异常', () => {
    // 生字是降级不是故障：那家仍能按中文搜到。
    expect(() => pinyinHit('魑魅魍魉', 'cm')).not.toThrow()
    expect(pinyinHit('魑魅魍魉', 'cm')).toBe(false)
  })

  it('搜索框：中文、字母、大小写都认', () => {
    expect(searchHit('高盛', '高')).toBe(true)
    expect(searchHit('高盛', 'GS')).toBe(true)
    expect(searchHit('高盛', 'gS')).toBe(true)
    expect(searchHit('黄金 AU', 'au')).toBe(true)
    expect(searchHit('黄金 AU', 'AU')).toBe(true)
    expect(searchHit('黄金 AU', '黄金')).toBe(true)
    // 拼音也该能找到品种：黄金 = hj。
    expect(searchHit('黄金 AU', 'hj')).toBe(true)
    expect(searchHit('黄金 AU', '白银')).toBe(false)
  })

  it('空查询放行全部', () => {
    expect(searchHit('随便什么', '')).toBe(true)
    expect(searchHit('随便什么', '   ')).toBe(true)
  })

  it('覆盖生产库里全部会员名用到的汉字', () => {
    // 这串字是从生产库 328 个归一后的会员名里提出来的全集（2026-08-15）。
    // 写这张表时漏过「君」「圳」「源」三个字——漏字不会报错，只是那几家搜不到，
    // 而搜不到时人会以为是自己拼错了，不会想到是表缺字。
    const PRODUCTION_CHARS =
      '一万三上世业东中丰久九乾云五亚交产京代伟佛信倍元先光公兴农冠冶创利前力勤北华南原友发台司合吉同君吴和商嘉团国地圳坤城塔大天奇子宁安宏宝实富尔山屿岭峰川州工平年广庆建弘德徽恒惠成投招控摩文新方无时昌易星晟晨智有期杉林根格正民永汇江沌河波泰津浙浦海深混渤港湖湘源澳烟煜牛物特珠理琪瑞甘生申电疆盛石矿祈神福科穗立第粤粮紫红纪经美联肃能航良色苏英茂莞蓬融行衍西证诚谷象豪财货贸越辉辰达迈运连迪通道邦部都重金鑫钢铜银锋锡锦长闻阳际陆陇陕雅集风首马高鲁鸿鹏黄黑鼎齐龙'
    const missing = [...PRODUCTION_CHARS].filter((char) => !hasInitial(char))
    expect(missing.join(''), '这些字不在表里').toBe('')
  })

  it('表里只放汉字', async () => {
    // 手写这张表时把英文单词粘进去过一次（'golden' 混进 j 组）。字母混进来不会
    // 报错，只会让那几个字母被当成汉字映射，静悄悄地污染匹配结果。
    const source = await import('./pinyin?raw').then((module) => module.default as string)
    const groups = source.match(/^\s{2}[a-z]: '([^']*)',?$/gm) ?? []
    expect(groups.length).toBe(22) // a..z 去掉 i、o、u、v
    for (const line of groups) {
      const chars = line.match(/'([^']*)'/)?.[1] ?? ''
      for (const char of chars) {
        expect(/[一-鿿]/.test(char), `「${char}」不是汉字：${line.trim()}`).toBe(true)
      }
    }
  })
})
