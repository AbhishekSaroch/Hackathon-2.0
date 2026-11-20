import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The frontend calls /bi02/..., /bi03/... and /health on its own origin; Vite forwards them to FastAPI.
// (API_PORT / WEB_PORT are only for running a second copy side by side; normally the defaults apply.)
const API = `http://127.0.0.1:${process.env.API_PORT || 8000}`
export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.WEB_PORT || 5173),
    proxy: {
      '/bi02': API,
      '/bi03': { target: API, timeout: 600000, proxyTimeout: 600000 },   // "advance one day" can take a while
      '/lc04': { target: API, timeout: 600000, proxyTimeout: 600000 },   // preparing the replay can take a minute
      '/lc01': { target: API, timeout: 120000, proxyTimeout: 120000 },   // a question can wait for the model's rate limit
      '/outreach': { target: API, timeout: 180000, proxyTimeout: 180000 }, // a bulk send waits for the providers
      '/health': API,
    },
  },
})
