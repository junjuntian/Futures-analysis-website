import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import { router } from './router'

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input.toString()
        const data = url.includes('/version')
          ? { name: 'futures-analysis-platform', version: 'test', git_sha: null }
          : { status: url.includes('/ready') ? 'ready' : 'ok' }

        return {
          ok: true,
          json: async () => ({ data, request_id: '00000000-0000-7000-8000-000000000000' })
        } as Response
      })
    )
  })

  it('renders the product title', async () => {
    router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      global: {
        plugins: [createPinia(), router, ElementPlus]
      }
    })

    expect(wrapper.text()).toContain('期货与套利数据分析平台')
  })
})
