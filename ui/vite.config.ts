import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: {
    strictPort: true,
    port: 1420,
  },
  build: {
    // Monterey's system WKWebView is based on Safari 15.1. Keep the
    // generated JavaScript within that baseline instead of relying on the
    // user's separately installed Safari version.
    target: "safari15.1",
  },
});
