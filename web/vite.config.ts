// vitest/config rather than vite: the `test` block below is Vitest's,
// and vite's own defineConfig does not know it exists.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // The app calls /v1 as a same-origin path, which is what a deployed
    // install serves. In development the API is a separate process, so the
    // dev server forwards those paths to it.
    //
    // Proxying rather than pointing the app at http://localhost:8000
    // directly keeps development and production on the same code path: no
    // CORS in one and not the other, and no base URL that is correct in
    // only one of them.
    proxy: {
      "/v1": {
        target: process.env.BIRYAR_API_URL ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
