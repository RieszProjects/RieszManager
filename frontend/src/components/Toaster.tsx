import { Alert, Portal, Snackbar } from "@mui/material";
import { useUi } from "@/store/ui";

export function Toaster() {
  const toasts = useUi((s) => s.toasts);
  const dismiss = useUi((s) => s.dismissToast);
  const current = toasts[0];
  // Mounted only while there is a toast: a portal that already exists when a dialog opens is marked
  // aria-hidden by it, which hides the alert from screen readers and the keyboard.
  if (!current) return null;
  return (
    <Portal>
      <Snackbar
        key={current?.id}
        open={Boolean(current)}
        autoHideDuration={current?.severity === "error" ? 6000 : 3000}
        onClose={(_, reason) => reason !== "clickaway" && current && dismiss(current.id)}
        anchorOrigin={{ vertical: "top", horizontal: "center" }}
        sx={{ top: "calc(12px + var(--safe-top)) !important" }}
      >
        {current ? (
          <Alert
            severity={current.severity}
            variant="outlined"
            onClose={() => dismiss(current.id)}
            sx={{ background: "var(--bg3)", color: "var(--text)", borderColor: "var(--line)", minWidth: 260, boxShadow: "0 12px 40px rgba(0,0,0,.4)" }}
          >
            {current.message}
          </Alert>
        ) : undefined}
      </Snackbar>
    </Portal>
  );
}
