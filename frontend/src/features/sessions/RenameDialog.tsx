import { Button, Dialog, DialogActions, DialogContent, DialogTitle, TextField } from "@mui/material";
import { useEffect, useState } from "react";
import { useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";

/** Single rename dialog for sessions, opened through `useUi().setRenameTarget`. */
export function RenameDialog() {
  const target = useUi((s) => s.renameTarget);
  const setTarget = useUi((s) => s.setRenameTarget);
  const renameSession = useApp((s) => s.renameSession);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (target) setValue(target.title);
  }, [target]);

  const close = () => !busy && setTarget(null);
  const submit = async () => {
    const title = value.trim();
    if (!target || !title || busy) return;
    if (title === target.title) {
      setTarget(null);
      return;
    }
    setBusy(true);
    try {
      await renameSession(target.sessionId, title);
      setTarget(null);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={Boolean(target)} onClose={close} fullWidth maxWidth="xs">
      <DialogTitle sx={{ fontFamily: "var(--font-display)", fontSize: 22, pb: 1 }}>重命名会话</DialogTitle>
      <DialogContent>
        <TextField
          autoFocus
          fullWidth
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onFocus={(e) => e.target.select()}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
          slotProps={{ htmlInput: { maxLength: 200 } }}
          sx={{ mt: 0.5 }}
        />
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={close} disabled={busy}>取消</Button>
        <Button variant="contained" onClick={() => void submit()} disabled={busy || !value.trim()}>保存</Button>
      </DialogActions>
    </Dialog>
  );
}
