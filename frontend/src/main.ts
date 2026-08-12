import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// Element Plus 默认英文。不配这个，日期选择器的月份与星期是 "February / Sun"，
// 整站只有那一处是英文。
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import './styles.css'

createApp(App)
  .use(createPinia())
  .use(router)
  .use(ElementPlus, { locale: zhCn })
  .mount('#app')
