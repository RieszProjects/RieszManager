import "@fontsource/cormorant-garamond/500.css";
import "@fontsource/cormorant-garamond/600.css";
import "@fontsource/cormorant-garamond/600-italic.css";
import "@fontsource-variable/inter";
import "@fontsource-variable/source-serif-4";
import "@fontsource-variable/source-serif-4/wght-italic.css";
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource/noto-serif-sc/600.css";
import "@fontsource-variable/jetbrains-mono";
import "./theme/global.css";
import "./theme/components.css";
import { CssBaseline, ThemeProvider } from "@mui/material";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { setupPwaUpdates } from "./lib/pwaUpdate";
import { theme } from "./theme/theme";
import { useApp } from "./store/app";
import { useSessions } from "./store/sessions";
import { useUi } from "./store/ui";

if (import.meta.env.DEV) {
  (window as unknown as { __rz: unknown }).__rz = { useApp, useSessions, useUi };
}

// iOS Safari ignores user-scalable=no; block its pinch-zoom gestures directly.
if (window.matchMedia("(pointer: coarse)").matches) {
  for (const type of ["gesturestart", "gesturechange", "gestureend"]) {
    document.addEventListener(type, (e) => e.preventDefault(), { passive: false });
  }
}

setupPwaUpdates();

// Keep the install prompt so the settings page can offer "install app" later.
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  (window as unknown as { __rzInstall?: Event }).__rzInstall = e;
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <App />
    </ThemeProvider>
  </StrictMode>,
);
