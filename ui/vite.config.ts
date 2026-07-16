import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: {
    strictPort: true,
    port: 1420,
  },
  build: {
    target: ["es2022", "chrome110", "safari16"],
  },
});
