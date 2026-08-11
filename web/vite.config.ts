import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// dev 阶段把 /api 代到后端，从而与后端同源 —— 本地调试不必配 CORS。
// 生产是独立静态站（另一个源），那时靠后端的 ADMIN_API_CORS_ORIGINS 放行。
const API_TARGET = process.env.VITE_DEV_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rolldownOptions: {
      output: {
        // 把 antd 与 react 拆出去：它们几乎不动，而业务代码每次发布都变。
        // 不拆的话改一行页面就让用户重下整个 1.2MB。
        codeSplitting: {
          groups: [
            { name: 'antd', test: /node_modules[\\/](antd|@ant-design|rc-)/ },
            { name: 'react', test: /node_modules[\\/](react|react-dom|react-router)/ },
          ],
        },
      },
    },
    // antd 单块就 1.1MB，是全量引入的必然结果，不是失控。阈值抬到它之上，
    // 让这条警告在真正失控时才响。
    chunkSizeWarningLimit: 1200,
  },
})
