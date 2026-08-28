import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import { resolve } from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ['src/**/*.spec.ts'],
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash].[ext]',
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('/vue/') || id.includes('/pinia/')) return 'framework';
          if (id.includes('/axios/')) return 'http';
          if (
            id.includes('/marked/') ||
            id.includes('/dompurify/') ||
            id.includes('/highlight.js/')
          ) {
            return 'markdown';
          }
          return 'vendor';
        },
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      // 代理后端接口，方便开发联调
      '/auth': 'http://localhost:8000',
      '/documents': 'http://localhost:8000',
      '/v1': 'http://localhost:8000',
    },
  },
});
