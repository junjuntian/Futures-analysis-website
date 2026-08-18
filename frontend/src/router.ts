import { createRouter, createWebHistory } from 'vue-router'
import AuthView from './views/AuthView.vue'
import HomeView from './views/HomeView.vue'
import NotFoundView from './views/NotFoundView.vue'
import { useAuthStore } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: HomeView },
    // /auth 是登录页,不在侧栏里出现:登录之后守卫会把它弹回首页,
    // 所以它当菜单项时点进去看到的就是首页——2026-08-13 运营者正是把这个
    // 现象报成「首页和身份认证界面一样」。路由必须留,菜单项已删。
    { path: '/auth', component: AuthView },
    { path: '/spread-analytics/monitor', component: () => import('./views/SpreadMonitorView.vue') },
    { path: '/spread-analytics/free-spread', component: () => import('./views/FreeSpreadView.vue') },
    { path: '/seats', component: () => import('./views/SeatsView.vue') },
    // 净持仓曾短暂做成独立页面（2026-08-18 上线当天即撤）：它与席位持仓共用
    // 顶部那组选择，分成两个页面就意味着选了席位过去还得再选一次。旧地址重定向
    // 到席位页的那个子页，运营者存的书签不至于落到 404。
    { path: '/seats/net-position', redirect: '/seats?tab=building' },
    { path: '/smart-money-view', component: () => import('./views/SmartMoneyView.vue') },
    // /sessions 与 /system 已退役:登录设备收进右上角账号菜单,
    // 服务健康并进总览页页脚。老书签落到 404,不再静默重定向。
    { path: '/:pathMatch(.*)*', component: NotFoundView }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.me && to.path !== '/auth') {
    await auth.refresh()
  }
  if (!auth.me && to.path !== '/auth') {
    return '/auth'
  }
  if (auth.me && to.path === '/auth') {
    return '/'
  }
  return true
})
