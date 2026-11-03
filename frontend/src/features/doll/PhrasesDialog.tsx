/** Editor for the doll's quick phrases. */
import { Button, Dialog, DialogActions, DialogContent, IconButton, TextField, Tooltip } from "@mui/material";
import { ArrowDown, ArrowUp, Plus, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useIsMobile } from "@/lib/useIsMobile";
import { toast } from "@/store/ui";
import { usePhrases } from "./phrases";

interface Row {
  key: number;
  text: string;
}

let rowKey = 0;

export function PhrasesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const isMobile = useIsMobile();
  const { phrases, load, save } = usePhrases();
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    load()
      .then(() => setRows(usePhrases.getState().phrases.map((p) => ({ key: ++rowKey, text: p.text }))))
      .catch((err) => toast((err as Error).message, "error"));
  }, [open, load]);

  const edit = (key: number, text: string) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, text } : r)));
  const move = (i: number, d: -1 | 1) =>
    setRows((rs) => {
      const next = [...rs];
      [next[i], next[i + d]] = [next[i + d], next[i]];
      return next;
    });
  const changed = rows.map((r) => r.text.trim()).filter(Boolean).join("\u0000") !== phrases.map((p) => p.text).join("\u0000");

  const submit = async () => {
    setBusy(true);
    try {
      await save(rows.map((r) => r.text));
      toast("快捷短语已保存", "success");
      onClose();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} fullScreen={isMobile} fullWidth maxWidth="sm">
      <div className="rz-settings-head">
        <span className="display">快捷短语</span>
        <IconButton size="small" onClick={onClose} aria-label="关闭"><X size={18} /></IconButton>
      </div>
      <DialogContent sx={{ pt: 1 }}>
        <p className="dim rz-phrases-hint">点莉思，再点“快捷短语”，选中的一条会填进当前的输入框。可以写多行。</p>
        <div className="rz-phrases-list">
          {rows.map((r, i) => (
            <div key={r.key} className="rz-phrases-row">
              <TextField
                fullWidth
                multiline
                minRows={1}
                maxRows={6}
                size="small"
                placeholder="例如：继续"
                value={r.text}
                onChange={(e) => edit(r.key, e.target.value)}
                autoFocus={!r.text && i === rows.length - 1}
              />
              <div className="rz-phrases-tools">
                <Tooltip title="上移"><span><IconButton size="small" disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp size={15} /></IconButton></span></Tooltip>
                <Tooltip title="下移"><span><IconButton size="small" disabled={i === rows.length - 1} onClick={() => move(i, 1)}><ArrowDown size={15} /></IconButton></span></Tooltip>
                <Tooltip title="删除"><IconButton size="small" onClick={() => setRows((rs) => rs.filter((x) => x.key !== r.key))}><Trash2 size={15} /></IconButton></Tooltip>
              </div>
            </div>
          ))}
          {rows.length === 0 && <div className="dim rz-phrases-empty">还没有快捷短语。</div>}
        </div>
        <Button startIcon={<Plus size={15} />} onClick={() => setRows((rs) => [...rs, { key: ++rowKey, text: "" }])} disabled={rows.length >= 100}>
          添加短语
        </Button>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button variant="outlined" onClick={onClose}>取消</Button>
        <Button variant="contained" disabled={busy || !changed} onClick={() => void submit()}>保存</Button>
      </DialogActions>
    </Dialog>
  );
}
