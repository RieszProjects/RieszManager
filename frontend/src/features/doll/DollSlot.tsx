/**
 * Mount point of the optional doll widget. The widget's code, styles and art
 * are only downloaded once it is switched on in the settings; while it is off
 * this renders nothing.
 */
import { lazy, Suspense } from "react";
import { useDollPrefs } from "./prefs";

const Doll = lazy(() => import("./Doll"));

export function DollSlot() {
  const enabled = useDollPrefs((s) => s.enabled);
  if (!enabled) return null;
  return (
    <Suspense fallback={null}>
      <Doll />
    </Suspense>
  );
}
