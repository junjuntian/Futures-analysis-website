import { describe, expect, it } from 'vitest'

import { failureHint } from './fetch-hint'

describe('failureHint', () => {
  it('403 明确指向权限,并**否掉**「引擎没跑」这个错方向', () => {
    // 这正是 2026-08-20 那次:引擎跑了,文件是 600。
    const hint = failureHint(new Error('HTTP 403'))
    expect(hint).toContain('权限')
    expect(hint).toContain('不是引擎没跑')
  })

  it('404 才是「有没有产出」的问题', () => {
    expect(failureHint(new Error('HTTP 404'))).toContain('引擎是否产出')
  })

  it('5xx 指向服务器,不指向前端', () => {
    expect(failureHint(new Error('HTTP 500'))).toContain('nginx')
    expect(failureHint(new Error('HTTP 502'))).toContain('nginx')
  })

  it('认不出状态码时不装懂 —— 不给任何笃定的单一原因', () => {
    const hint = failureHint(new TypeError('Failed to fetch'))
    expect(hint).toContain('控制台')
    expect(hint).not.toContain('引擎')
    expect(hint).not.toContain('权限')
  })
})
