import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the engine runs separately (`dynacamp serve ...`, API on :8080):
// Vite proxies /api there, so the browser sees a single origin (SSE included).
const API = process.env.DYNACAMP_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      // MapLibre and milsymbol (all NATO symbol sets) are most of the bundle:
      // keep them in their own long-cached chunks, the app code stays small.
      output: {
        advancedChunks: {
          groups: [
            { name: "maplibre", test: /node_modules[\\/]maplibre-gl/ },
            { name: "milsymbol", test: /node_modules[\\/]milsymbol/ },
          ],
        },
      },
    },
    chunkSizeWarningLimit: 1100,
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: API, changeOrigin: true } },
  },
});
