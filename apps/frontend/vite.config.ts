import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Local dev proxies API calls to the incident API; Compose serves same-origin
// via nginx so VITE_INCIDENT_API_URL stays empty in production builds.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_API_URL ?? "http://127.0.0.1:8006",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
