import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const backend = process.env.RIESZ_DEV_BACKEND ?? "http://127.0.0.1:18601";

export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  plugins: [
    react(),
    VitePWA({
      // Registered from src/lib/pwaUpdate.ts, which decides when to switch to a new version.
      registerType: "prompt",
      injectRegister: false,
      includeAssets: ["favicon.png", "icons/apple-touch-icon.png"],
      manifest: {
        id: "/",
        name: "Riesz",
        short_name: "Riesz",
        description: "Claude Code 会话管理",
        lang: "zh-CN",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "any",
        background_color: "#121513",
        theme_color: "#121513",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "/icons/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//],
        // Fonts are split into many unicode-range slices; cache them lazily instead of precaching.
        globPatterns: ["**/*.{js,css,html,png,svg,webp}"],
        // The optional doll widget's art is fetched only when it is switched on.
        globIgnores: ["doll/**", "push-sw.js"],
        // Push notification handlers (shown and tapped), kept as a plain file in public/.
        importScripts: ["/push-sw.js"],
        maximumFileSizeToCacheInBytes: 6 * 1024 * 1024,
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/assets/") && /\.(woff2?|ttf)$/.test(url.pathname),
            handler: "CacheFirst",
            options: { cacheName: "fonts", expiration: { maxEntries: 400, maxAgeSeconds: 60 * 60 * 24 * 365 } },
          },
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/doll/"),
            handler: "StaleWhileRevalidate",
            options: { cacheName: "doll", expiration: { maxEntries: 40 } },
          },
        ],
      },
    }),
  ],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    target: "es2022",
    chunkSizeWarningLimit: 1500,
  },
});
