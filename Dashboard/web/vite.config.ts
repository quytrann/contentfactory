import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Forward API + generation + media calls to the local FastAPI server (Dashboard/api).
    proxy: {
      '/api': { target: 'http://127.0.0.1:4000', changeOrigin: true },
      '/generate': { target: 'http://127.0.0.1:4000', changeOrigin: true },
      '/media': { target: 'http://127.0.0.1:4000', changeOrigin: true },
    },
  },
})
