/** Composer toolbar chip to bind/unbind an online local computer node to the active session. */
import { Divider, ListItemText, ListSubheader, Menu, MenuItem } from "@mui/material";
import { Check, ChevronDown, Laptop, Link2Off, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { toast } from "@/store/ui";
import { NODE_MODE_LABEL, type NodeMode, useNodes } from "./store";

const ALL_MODES: NodeMode[] = ["readonly", "ask", "auto", "full"];

export function NodeBindChip({ sessionId }: { sessionId: string }) {
  const devices = useNodes((s) => s.devices);
  const bind = useNodes((s) => s.bind);
  const unbind = useNodes((s) => s.unbind);
  const setOpen = useNodes((s) => s.setOpen);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [pending, setPending] = useState(false);

  const online = devices.filter((d) => d.online);
  const bound = online
    .map((d) => ({ device: d, binding: d.bindings.find((b) => b.sessionId === sessionId) }))
    .filter((x): x is { device: typeof online[number]; binding: NonNullable<typeof x.binding> } => Boolean(x.binding));

  if (online.length === 0 && bound.length === 0) return null;

  const doBind = async (nodeId: string, mode: NodeMode) => {
    setAnchor(null);
    setPending(true);
    try {
      await bind(nodeId, sessionId, mode);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setPending(false);
    }
  };

  const doUnbind = async (nodeId: string) => {
    setAnchor(null);
    setPending(true);
    try {
      await unbind(nodeId, sessionId);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setPending(false);
    }
  };

  const label = pending
    ? "等待电脑确认…"
    : bound.length === 1
      ? `${bound[0].device.name} · ${NODE_MODE_LABEL[bound[0].binding.mode]}`
      : bound.length > 1
        ? `已绑 ${bound.length} 台电脑`
        : `绑定电脑 (${online.length})`;

  return (
    <>
      <button
        type="button"
        className={`rz-chip${bound.length > 0 ? " rz-node-bound" : ""}`}
        disabled={pending}
        onClick={(e) => setAnchor(e.currentTarget)}
        title={bound.length > 0 ? "本会话已绑定本地电脑节点（点击切换权限或解绑）" : "将在线的本地电脑节点绑定到此会话"}
      >
        {pending ? <LoaderCircle size={13} className="spin" /> : <Laptop size={13} />}
        <span>{label}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}>
        {online.map((d) => {
          const cur = d.bindings.find((b) => b.sessionId === sessionId);
          const maxRank = ALL_MODES.indexOf(d.maxMode ?? "auto");
          const allowed = ALL_MODES.slice(0, maxRank + 1);
          return [
            <ListSubheader key={`h-${d.id}`} sx={{ lineHeight: "28px", fontSize: 12 }}>
              {d.name} · {d.os || d.platform}
            </ListSubheader>,
            ...allowed.map((m) => (
              <MenuItem key={`${d.id}-${m}`} selected={cur?.mode === m} onClick={() => void doBind(d.id, m)}>
                <ListItemText
                  primary={cur?.mode === m ? `已绑定：${NODE_MODE_LABEL[m]}` : `以「${NODE_MODE_LABEL[m]}」绑定`}
                  secondary={m === "auto" ? "本地硬规则 + 模型风控，高危需确认" : m === "readonly" ? "仅只读查询和拉取文件" : m === "ask" ? "每条指令/推送都需确认" : "允许目录内全自动执行"}
                  slotProps={{ secondary: { sx: { fontSize: 11.5 } } }}
                />
                {cur?.mode === m && <Check size={14} />}
              </MenuItem>
            )),
            cur && (
              <MenuItem key={`u-${d.id}`} onClick={() => void doUnbind(d.id)} sx={{ color: "error.main" }}>
                <Link2Off size={14} style={{ marginRight: 8 }} />
                解除绑定 {d.name}
              </MenuItem>
            ),
            <Divider key={`d-${d.id}`} />,
          ];
        })}
        <MenuItem onClick={() => { setAnchor(null); setOpen(true); }}>
          <ListItemText primary="管理全部本地节点…" />
        </MenuItem>
      </Menu>
    </>
  );
}
