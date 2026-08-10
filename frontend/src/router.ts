import { createRouter, createWebHistory } from 'vue-router'
import AuthView from './views/AuthView.vue'
import HomeView from './views/HomeView.vue'
import ImportCenterView from './views/ImportCenterView.vue'
import NotFoundView from './views/NotFoundView.vue'
import SessionsView from './views/SessionsView.vue'
import SystemView from './views/SystemView.vue'
import { useAuthStore } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: HomeView },
    { path: '/auth', component: AuthView },
    { path: '/imports', component: ImportCenterView },
    { path: '/spread-analytics/free-spread', component: () => import('./views/FreeSpreadView.vue') },
    { path: '/seats', component: () => import('./views/SeatsView.vue') },
    { path: '/sessions', component: SessionsView },
    { path: '/system', component: SystemView },
    { path: '/:pathMatch(.*)*', component: NotFoundView }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.me && to.path !== '/auth') {
    await auth.refresh()
  }
  if (!auth.me && to.path !== '/auth' && to.path !== '/system') {
    return '/auth'
  }
  if (auth.me && to.path === '/auth') {
    return '/'
  }
  return true
})
