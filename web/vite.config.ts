import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Dev: `npm run dev` proxies API + WebSocket to the engine on :8765.
// Prod: `npm run build` → web/dist, served by FastAPI at the same origin.
export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
});
