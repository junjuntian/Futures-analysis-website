import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// `--mode proddata`(npm run dev:proddata):本地改样式、数据取生产。
// 视觉改版没有本地 API 时,空态页面验不了表格与图表,只能这么看真实效果。
//
// 两个绕不开的点(2026-08-16 实测):
// 1. 本机系统 DNS 对 shejimao.trade 返回污染 IP(93.46.8.90 等),Node 的
//    getaddrinfo 中招而浏览器走安全 DNS 无感——代理必须直连生产真实 IP。
//    生产换机要同步改这里(当前 qh=172.238.18.206)。
// 2. 证书按域名签发,直连 IP 校验必失败,secure:false 跳过——仅本地开发代理,
//    连的是自己的服务器,可接受。Host/Origin/Referer 改写成生产域名,否则
//    后端 PUBLIC_ORIGIN 校验会拒。Chrome 对 http://localhost 放行 Secure
//    cookie,所以会话 cookie 存得住。
const PROD_HOST = 'shejimao.trade'
const PROD_IP = 'https://172.238.18.206'

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: mode === 'proddata'
      // '/smart-money/' 必须带尾斜杠:裸前缀会把 SPA 路由 /smart-money-view
      // 一并代理到生产,页面直接白屏(2026-08-16 实测踩坑)。
      ? Object.fromEntries(['/api', '/smart-money/'].map((path) => [path, {
          target: PROD_IP,
          secure: false,
          cookieDomainRewrite: '',
          configure(proxy: { on(event: string, handler: (proxyReq: { setHeader(name: string, value: string): void }) => void): void }) {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('host', PROD_HOST)
              proxyReq.setHeader('origin', `https://${PROD_HOST}`)
              proxyReq.setHeader('referer', `https://${PROD_HOST}/`)
            })
          }
        }]))
      : { '/api': 'http://localhost:8080' }
  },
  test: {
    environment: 'jsdom'
  }
}))
