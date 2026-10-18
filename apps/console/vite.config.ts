import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The console is an offline demonstration: relative asset paths, no external origins,
// no runtime network calls. See README.md and scripts/check-offline-build.mjs.
export default defineConfig({
  base: "./",
  plugins: [react()],
  // modulePreload polyfill disabled: it injects a fetch() call, and every target browser
  // (ES2022) supports <link rel="modulepreload"> natively.
  build: { target: "es2022", sourcemap: false, modulePreload: { polyfill: false } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
