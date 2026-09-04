import { defineConfig } from "@playwright/test";

const baseURL = process.env.FRONTEND_E2E_URL ?? "http://127.0.0.1:5173";

// Minimal browser E2E: by default the spec fulfills /api/* with fixtures so it
// runs without the full Compose stack. Point FRONTEND_E2E_URL at a live
// frontend and FRONTEND_E2E_LIVE=1 at a live stack to exercise the real API.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL },
});
