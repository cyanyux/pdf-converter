import react from "@vitejs/plugin-react";
import { defineConfig } from "vite-plus";

// SPA dev server proxies /api to the Hono server; production build emits static
// assets served by that same server. Standard Vite fields (plugins/server/build)
// sit alongside Vite+ blocks.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
