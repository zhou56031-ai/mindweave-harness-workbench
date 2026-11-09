import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const BACKEND_TARGET = process.env.VITE_BACKEND_TARGET || 'http://127.0.0.1:8100'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    // 首屏与包体：图谱、编辑器、Markdown 拆成独立 chunk
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('@antv/g6') || id.includes('d3-force')) return 'graph'
          if (id.includes('@tiptap')) return 'editor'
          if (id.includes('react-markdown') || id.includes('rehype') || id.includes('remark')) return 'markdown'
          return undefined
        },
      },
    },
  },
  server: {
    port: 3100,
    host: '127.0.0.1',
    proxy: {
      '/chat/agent/': { target: BACKEND_TARGET, changeOrigin: true, ws: true },
      '/chat/rag/': { target: BACKEND_TARGET, changeOrigin: true },
      '/chat/session/': { target: BACKEND_TARGET, changeOrigin: true },
      '/chat/sessions': { target: BACKEND_TARGET, changeOrigin: true },
      '/chat/usage': { target: BACKEND_TARGET, changeOrigin: true },
      '/chat/reorder': { target: BACKEND_TARGET, changeOrigin: true },
      '/knowledge/': { target: BACKEND_TARGET, changeOrigin: true },
      '/note/': { target: BACKEND_TARGET, changeOrigin: true },
      '/note-template/': { target: BACKEND_TARGET, changeOrigin: true },
      '/review/': { target: BACKEND_TARGET, changeOrigin: true },
      '/health': { target: BACKEND_TARGET, changeOrigin: true },
      '/user': { target: BACKEND_TARGET, changeOrigin: true },
      '/file': { target: BACKEND_TARGET, changeOrigin: true },
      '/media': { target: BACKEND_TARGET, changeOrigin: true },
      '/config': { target: BACKEND_TARGET, changeOrigin: true },
      '/api/graph': { target: BACKEND_TARGET, changeOrigin: true },
    },
  },
})
