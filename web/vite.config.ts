import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  // Keep browser requests same-origin, including when an old .env.local exists.
  define: { 'import.meta.env.VITE_API_BASE': JSON.stringify('/api') },
  server: {
    port: 5174, host: '127.0.0.1',
    proxy: { '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true, rewrite: path => path.replace(/^\/api/, '') } },
  },
});
