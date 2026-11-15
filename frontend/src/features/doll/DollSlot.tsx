/**
 * Mount point of the optional doll widget. The widget's code, styles and art
 * are only downloaded once it is switched on in the settings; while it is off
 * this renders nothing.
 */
import { lazy, Suspense, type ComponentType } from "react";
import { useDollPrefs } from "./prefs";

// If her code cannot be fetched (offline, or a new version replaced the files), go without her rather than fail the app.
const Doll = lazy<ComponentType>(() => import("./Doll").catch(() => ({ default: () => null })));

export function DollSlot() {
  const enabled = useDollPrefs((s) => s.enabled);
  if (!enabled) return null;
  return (
    <Suspense fallback={null}>
      <Doll />
    </Suspense>
  );
}
