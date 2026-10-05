import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发态把 /api 打到本机后端（README: 后端可手动起在 8080）。
// 生产构建产物由后端 StaticFiles 托管在 /，因此 base 用 './' 之外的绝对 '/' 均可；
// 这里保持默认 '/'，后端 mount('/') 时资源路径 /assets/xxx 能正确命中。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
})
