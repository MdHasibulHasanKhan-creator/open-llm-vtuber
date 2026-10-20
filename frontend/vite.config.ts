import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * The dev server proxies `/api` and `/ws` to the Python backend, so the browser
 * always talks to a same-origin WebSocket. In production the backend serves the
 * built assets itself (`VTUBER_SERVER__SERVE_FRONTEND=true`), which keeps the
 * very same same-origin contract - no CORS, no URL juggling in the client.
 */
const BACKEND = process.env.VITE_BACKEND ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND.replace(/^http/, 'ws'), ws: true, changeOrigin: true },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
    // three + three-vrm is a chunky dependency; keep the warning quiet.
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        // Function form: the object form is no longer in Rollup's types.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('@pixiv/three-vrm')) return 'vrm';
          if (id.includes('three')) return 'three';
          if (id.includes('react')) return 'react';
          return undefined;
        },
      },
    },
  },
  optimizeDeps: {
    include: ['three', '@pixiv/three-vrm'],
  },
});
