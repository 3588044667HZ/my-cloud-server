import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import '../shared/base.css'
import DriveHome from './DriveHome.vue'

createApp(DriveHome).use(ElementPlus, { locale: zhCn }).mount('#app')
