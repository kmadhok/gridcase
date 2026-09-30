/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/marketcase/", // = src/basePath.ts; the server's test_m8 checks the built page uses it
  server: {
    proxy: {
      "/marketcase/api": "http://127.0.0.1:8710",
      "/mcp": "http://127.0.0.1:8710",
    },
  },
  // maplibre-gl is dynamically imported in MapView, so it is already its own lazy chunk;
  // a manualChunks split breaks its worker URL (new Worker(new URL(..., import.meta.url))).
  build: { chunkSizeWarningLimit: 1200 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
