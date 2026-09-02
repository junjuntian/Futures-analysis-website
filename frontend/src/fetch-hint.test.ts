import { describe, expect, it } from 'vitest'

import { failureHint, isNetworkFailure } from './fetch-hint'

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
    const hint = failureHint(new Error('something went sideways'))
    expect(hint).toContain('控制台')
    expect(hint).not.toContain('引擎')
    expect(hint).not.toContain('权限')
  })

  // 2026-09-02 自由价差页:`Failed to fetch`,而两级 nginx 日志一行都没有。
  // 「请求没送到」是能确知的事实,不是猜 —— 这一类要说清楚,免得跑去翻服务器日志。
  it('网络层失败要说明「没送到服务器」,并提醒日志里查不到', () => {
    const hint = failureHint(new TypeError('Failed to fetch'))
    expect(hint).toContain('没送到服务器')
    expect(hint).toContain('日志')
    expect(hint).not.toContain('权限')
  })

  it('各家浏览器的说法都要认得', () => {
    for (const message of ['Failed to fetch', 'Load failed',
      'NetworkError when attempting to fetch resource.']) {
      expect(isNetworkFailure(new TypeError(message))).toBe(true)
    }
  })

  it('**自己代码里的 TypeError 不算网络失败** —— 否则又是一次指错方向', () => {
    expect(isNetworkFailure(new TypeError("Cannot read properties of undefined"))).toBe(false)
    expect(isNetworkFailure(new Error('HTTP 500'))).toBe(false)
  })
})
