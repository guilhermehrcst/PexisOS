import { defineConfig, type Plugin } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Content-Security-Policy for the built page: a second, independent barrier behind the
 * offline guard. Even if a network call slipped into the bundle, the browser would refuse
 * it (connect-src 'none') and refuse any external script, style, font or image.
 * Build only: the Vite dev server needs a websocket and inline scripts for HMR.
 */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'", // React style attributes (positions, transforms)
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "pexisos-console-csp",
    apply: "build",
    transformIndexHtml: () => [
      { tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: CONTENT_SECURITY_POLICY }, injectTo: "head-prepend" },
    ],
  };
}

// The console is an offline demonstration: relative asset paths, no external origins,
// no runtime network calls. See README.md and scripts/check-offline-build.mjs.
export default defineConfig({
  base: "./",
  plugins: [react(), contentSecurityPolicy()],
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
