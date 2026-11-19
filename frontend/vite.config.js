import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The frontend calls /bi02/... and /health on its own origin; Vite forwards them to FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/bi02': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
