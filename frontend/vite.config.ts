import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Die Oberfläche wird lokal betrieben. Im Entwicklungsmodus wird /api an das
// FastAPI-Backend weitergeleitet, damit kein CORS-Sonderfall nötig ist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
