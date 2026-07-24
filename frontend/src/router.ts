import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import NotFoundView from './views/NotFoundView.vue'
import SystemView from './views/SystemView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: HomeView },
    { path: '/system', component: SystemView },
    { path: '/:pathMatch(.*)*', component: NotFoundView }
  ]
})
