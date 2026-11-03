/** Offers a newly deployed version of the app (see lib/pwaUpdate). */
import { Button, Snackbar } from "@mui/material";
import { useAppUpdate } from "@/lib/pwaUpdate";

export function UpdateBanner() {
  const { ready, apply } = useAppUpdate();
  return (
    <Snackbar
      open={ready}
      anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      sx={{ bottom: "calc(16px + var(--safe-bottom)) !important" }}
      message="有新版本"
      action={<Button size="small" onClick={apply}>刷新</Button>}
      slotProps={{ content: { sx: { background: "var(--bg3)", color: "var(--text)", border: "1px solid var(--line)" } } }}
    />
  );
}
