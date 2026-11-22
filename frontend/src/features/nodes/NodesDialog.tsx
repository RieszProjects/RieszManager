/** Management dialog for pairing, inspecting, kicking/revoking, and monitoring local computer nodes. */
import {
  Button, Checkbox, Dialog, DialogContent, DialogTitle, FormControlLabel, IconButton, TextField, Tooltip,
} from "@mui/material";
import { Check, Copy, Laptop, Link2Off, Plus, Power, RefreshCw, ShieldAlert, Square, Trash2, X } from "lucide-react";
import { useState } from "react";
import { copyText } from "@/lib/clipboard";
import { toast } from "@/store/ui";
import { NODE_MODE_LABEL, type NodeCommand, type NodeDevice, useNodes } from "./store";

const RISK_TEXT: Record<string, string> = { low: "低风险", medium: "中风险", high: "高风险" };

function CmdItem({ cmd, onKill }: { cmd: NodeCommand; onKill: () => void }) {
  const [expanded, setExpanded] = useState(cmd.status === "running");
  return (
    <div className={`rz-node-cmd status-${cmd.status}`}>
      <div className="rz-node-cmd-head" onClick={() => setExpanded((e) => !e)}>
        <span className="rz-node-cmd-status">{cmd.status === "running" ? "⏳" : cmd.status === "done" ? "✓" : cmd.status === "killed" ? "⊘" : "✗"}</span>
        <span className="mono clamp-1 rz-node-cmd-text">{cmd.command}</span>
        <span className="dim">{cmd.sessionTitle}</span>
        {cmd.status === "running" && (
          <Tooltip title="终止该命令">
            <IconButton size="small" onClick={(e) => { e.stopPropagation(); onKill(); }} className="rz-danger">
              <Square size={12} />
            </IconButton>
          </Tooltip>
        )}
      </div>
      {expanded && (
        <div className="rz-node-cmd-body">
          {cmd.riskReason && <div className="dim">风控（{RISK_TEXT[cmd.risk] ?? cmd.risk}）：{cmd.riskReason}</div>}
          <pre className="mono rz-node-cmd-out">{cmd.output || "（暂无输出）"}</pre>
        </div>
      )}
    </div>
  );
}

function DeviceCard({ d }: { d: NodeDevice }) {
  const kick = useNodes((s) => s.kick);
  const revoke = useNodes((s) => s.revoke);
  const remove = useNodes((s) => s.remove);
  const unbind = useNodes((s) => s.unbind);
  const killCommand = useNodes((s) => s.killCommand);
  const cmds = [...d.runningCommands, ...d.recentCommands.slice().reverse()].slice(0, 8);

  return (
    <div className={`rz-node-card${d.online ? " online" : ""}${d.revoked ? " revoked" : ""}`}>
      <div className="rz-node-card-head">
        <span className={`rz-node-dot${d.online ? " on" : ""}`} />
        <div className="rz-node-card-title">
          <strong>{d.name}</strong>
          <span className="dim">
            {d.os || d.platform || "未知系统"}
            {d.ephemeral ? " · 临时" : ""}
            {d.revoked ? " · 已吊销" : d.online ? ` · 权限上限 ${NODE_MODE_LABEL[d.maxMode ?? "auto"]}` : " · 离线"}
          </span>
        </div>
        <div className="rz-node-card-actions">
          {d.online && (
            <Tooltip title="断开连接">
              <IconButton size="small" onClick={() => void kick(d.id)}><Power size={15} /></IconButton>
            </Tooltip>
          )}
          {!d.revoked && !d.ephemeral && (
            <Tooltip title="吊销设备密钥">
              <IconButton size="small" onClick={() => void revoke(d.id)}><ShieldAlert size={15} /></IconButton>
            </Tooltip>
          )}
          <Tooltip title="删除记录">
            <IconButton size="small" className="rz-danger" onClick={() => void remove(d.id)}><Trash2 size={15} /></IconButton>
          </Tooltip>
        </div>
      </div>

      {d.online && (
        <div className="rz-node-meta">
          {d.note && <div>备注：{d.note}</div>}
          <div className="mono dim">
            {d.dirs.length ? `默认目录：${d.dirs.join("  |  ")}` : "目录授权：按绑定会话在电脑端单独选择"}
          </div>
        </div>
      )}

      {d.bindings.length > 0 && (
        <div className="rz-node-bindings">
          <span className="dim">已绑定会话：</span>
          {d.bindings.map((b) => (
            <span key={b.sessionId} className="rz-node-bind-tag" title={b.dirs?.length ? `允许目录：${b.dirs.join(" ; ")}` : undefined}>
              <span>
                {b.sessionTitle} ({NODE_MODE_LABEL[b.mode]}
                {b.dirs?.length ? ` · ${b.dirs.join(", ")}` : ""})
              </span>
              <button type="button" title="解绑" onClick={() => void unbind(d.id, b.sessionId)}><Link2Off size={11} /></button>
            </span>
          ))}
        </div>
      )}

      {cmds.length > 0 && (
        <div className="rz-node-cmds">
          {cmds.map((c) => <CmdItem key={c.id} cmd={c} onKill={() => void killCommand(d.id, c.id)} />)}
        </div>
      )}
    </div>
  );
}

export function NodesDialog() {
  const open = useNodes((s) => s.open);
  const setOpen = useNodes((s) => s.setOpen);
  const devices = useNodes((s) => s.devices);
  const approvals = useNodes((s) => s.approvals);
  const pairings = useNodes((s) => s.pairings);
  const downloadToken = useNodes((s) => s.downloadToken);
  const downloadTokenPinned = useNodes((s) => s.downloadTokenPinned);
  const rotateDownloadToken = useNodes((s) => s.rotateDownloadToken);
  const createPairing = useNodes((s) => s.createPairing);
  const resolveApproval = useNodes((s) => s.resolveApproval);

  const [label, setLabel] = useState("");
  const [ephemeral, setEphemeral] = useState(false);
  const [busy, setBusy] = useState(false);

  const origin = typeof window !== "undefined" ? window.location.origin : "https://your-server";
  const bootstrapUrl = downloadToken
    ? `${origin}/api/nodes/bootstrap.py?token=${encodeURIComponent(downloadToken)}`
    : `${origin}/api/nodes/bootstrap.py?token=<loading>`;
  const oneLiner = `python -c "import urllib.request as u; exec(u.urlopen(u.Request('${bootstrapUrl}', headers={'User-Agent': 'riesz-node/1'})).read())"`;

  const onCreatePair = async () => {
    setBusy(true);
    try {
      await createPairing(label, ephemeral);
      setLabel("");
      toast("已生成 5 分钟有效的一次性配对码", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const copyCmd = (cmd: string) => {
    void copyText(cmd);
    toast("命令已复制", "success");
  };

  return (
    <Dialog open={open} onClose={() => setOpen(false)} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, pr: 6 }}>
        <Laptop size={18} />
        <span>本地电脑节点（riesz-node）</span>
        <IconButton size="small" onClick={() => setOpen(false)} sx={{ position: "absolute", right: 12, top: 12 }}>
          <X size={18} />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers className="rz-nodes-dialog">
        {approvals.length > 0 && (
          <section className="rz-nodes-section">
            <h4>待审批指令（{approvals.length}）</h4>
            {approvals.map((ap) => (
              <div key={ap.id} className="rz-node-approval-card">
                <div className="rz-node-approval-head">
                  <strong>[{ap.nodeName}]</strong>
                  <span className="dim">会话：{ap.sessionTitle}</span>
                  <span className={`rz-tag risk-${ap.risk}`}>{RISK_TEXT[ap.risk] ?? ap.risk}</span>
                </div>
                <pre className="mono rz-node-approval-cmd">{ap.command}</pre>
                <div className="dim">{ap.reason}</div>
                <div className="rz-node-approval-actions">
                  <Button size="small" variant="contained" startIcon={<Check size={14} />}
                    onClick={() => void resolveApproval(ap.id, true)}>允许</Button>
                  <Button size="small" variant="outlined" color="error" startIcon={<X size={14} />}
                    onClick={() => void resolveApproval(ap.id, false)}>拒绝</Button>
                </div>
              </div>
            ))}
          </section>
        )}

        <section className="rz-nodes-section">
          <h4>配对新电脑</h4>
          <p className="dim" style={{ margin: "0 0 10px", fontSize: 12.5 }}>
            电脑端主动发起加密反向连接（Ed25519 验签），断开即解绑；启动无需预选目录，可在电脑端界面里为每个绑定会话单独选目录。
          </p>
          <div className="rz-node-pair-box" style={{ marginTop: 0, marginBottom: 12 }}>
            <div className="rz-node-pair-code">
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>跨平台一键启动（含下载口令，未带口令返回 404；可存进快捷脚本）</span>
              <span className="dim">需 Python 3.10+</span>
            </div>
            <div className="rz-node-pair-cmd">
              <code className="mono">{oneLiner}</code>
              <Tooltip title="复制一键启动命令">
                <span>
                  <IconButton size="small" disabled={!downloadToken} onClick={() => copyCmd(oneLiner)}>
                    <Copy size={14} />
                  </IconButton>
                </span>
              </Tooltip>
              {!downloadTokenPinned && (
                <Tooltip title="重置下载口令（旧链接将立即失效）">
                  <IconButton size="small" onClick={() => void rotateDownloadToken().catch((e) => toast((e as Error).message, "error"))}>
                    <RefreshCw size={14} />
                  </IconButton>
                </Tooltip>
              )}
            </div>
          </div>
          <div className="rz-nodes-pair-form">
            <TextField size="small" label="设备名称（可选）" placeholder="如 win-pc / macbook"
              value={label} onChange={(e) => setLabel(e.target.value)} sx={{ flex: 1 }} />
            <FormControlLabel
              control={<Checkbox size="small" checked={ephemeral} onChange={(e) => setEphemeral(e.target.checked)} />}
              label={<span style={{ fontSize: 12.5 }}>临时模式（断开即删）</span>}
            />
            <Button variant="contained" size="small" startIcon={<Plus size={15} />} disabled={busy} onClick={onCreatePair}>
              生成配对码
            </Button>
          </div>

          {pairings.map((p) => {
            const extra = p.ephemeral
              ? `--code ${p.code} --ephemeral`
              : `--code ${p.code}${p.label ? ` --name "${p.label}"` : ""}`;
            const cmd = `${oneLiner} -- ${extra}`;
            return (
              <div key={p.code} className="rz-node-pair-box">
                <div className="rz-node-pair-code">
                  <span className="mono">{p.code}</span>
                  <span className="dim">{p.ephemeral ? "临时 · 一次性" : "永久配对 · 一次性"} · 5 分钟有效</span>
                </div>
                <div className="rz-node-pair-cmd">
                  <code className="mono">{cmd}</code>
                  <Tooltip title="复制命令">
                    <IconButton size="small" onClick={() => copyCmd(cmd)}><Copy size={14} /></IconButton>
                  </Tooltip>
                </div>
              </div>
            );
          })}
        </section>

        <section className="rz-nodes-section">
          <h4>已注册设备（{devices.filter((d) => d.online).length} 在线 / {devices.length} 总计）</h4>
          {devices.length === 0 ? (
            <div className="dim" style={{ fontSize: 13 }}>还没有配对任何本地电脑。生成配对码并在电脑上运行 `riesz-node connect` 即可连上来。</div>
          ) : (
            <div className="rz-nodes-list">
              {devices.map((d) => <DeviceCard key={d.id} d={d} />)}
            </div>
          )}
        </section>
      </DialogContent>
    </Dialog>
  );
}
