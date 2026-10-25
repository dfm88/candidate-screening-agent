import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // In dev the API runs separately (uvicorn on :8000); in the container FastAPI serves both.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
