import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import '../shared/base.css'
import AlbumHome from './AlbumHome.vue'

createApp(AlbumHome).use(ElementPlus, { locale: zhCn }).mount('#app')
