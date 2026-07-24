import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import { router } from './router'

describe('App', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input.toString()
        if (url.includes('/auth/me') || url.includes('/workspace')) {
          return {
            ok: false,
            status: 401,
            json: async () => ({})
          } as Response
        }
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
    const pinia = createPinia()
    setActivePinia(pinia)
    router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      global: {
        plugins: [pinia, router, ElementPlus]
      }
    })

    expect(wrapper.text()).toContain('期货与套利数据分析平台')
  })
})
