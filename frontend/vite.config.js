import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  envDir: '..', // VITE_AG_LICENSE_KEY lives in the root .env; only VITE_* variables reach the browser
  server: { proxy: { '/api': 'http://localhost:8000' } },
})
