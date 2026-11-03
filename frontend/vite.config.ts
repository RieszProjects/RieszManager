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
      registerType: "autoUpdate",
      injectRegister: "auto",
      includeAssets: ["favicon.png", "icons/apple-touch-icon.png"],
      manifest: {
        name: "RieszManager",
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
        maximumFileSizeToCacheInBytes: 6 * 1024 * 1024,
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/assets/") && /\.(woff2?|ttf)$/.test(url.pathname),
            handler: "CacheFirst",
            options: { cacheName: "fonts", expiration: { maxEntries: 400, maxAgeSeconds: 60 * 60 * 24 * 365 } },
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
