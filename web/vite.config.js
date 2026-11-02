import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// MPA：portal / pair / album / drive，base=/web/（与笔记前端 /assets 隔离）
export default defineConfig({
  plugins: [vue()],
  base: '/web/',
  server: {
    port: 3003,
    proxy: {
      '/api': { target: 'http://127.0.0.1:5000', changeOrigin: true },
    }
  },
  build: {
    outDir: '../static/web',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        portal: resolve(__dirname, 'portal.html'),
        pair: resolve(__dirname, 'pair.html'),
        album: resolve(__dirname, 'album.html'),
        drive: resolve(__dirname, 'drive.html'),
      },
      output: {
        manualChunks: {
          'element-plus': ['element-plus'],
        },
      },
    },
  },
})
