/** Session-scoped dialog for pairing, renaming, monitoring, and disconnecting local computer nodes. */
import {
  Button, Dialog, DialogContent, DialogTitle, IconButton, MenuItem, Select, TextField, Tooltip,
} from "@mui/material";
import { Check, Copy, Laptop, Pencil, Plus, Power, RefreshCw, Square, X } from "lucide-react";
import { useState } from "react";
import { copyText } from "@/lib/clipboard";
import { sessionTitle as getSessionTitle, useApp } from "@/store/app";
import { toast } from "@/store/ui";
import { NODE_MODE_LABEL, type LiveNodeInfo, type NodeCommand, type NodeMode, useNodes } from "./store";

const ALL_MODES: NodeMode[] = ["readonly", "ask", "auto", "full"];
const RISK_TEXT: Record<string, string> = { low: "低风险", medium: "中风险", high: "高风险" };

function CmdItem({ cmd, onKill }: { cmd: NodeCommand; onKill: () => void }) {
  const [expanded, setExpanded] = useState(cmd.status === "running");
  return (
    <div className={`rz-node-cmd status-${cmd.status}`}>
      <div className="rz-node-cmd-head" onClick={() => setExpanded((e) => !e)}>
        <span className="rz-node-cmd-status">
          {cmd.status === "running" ? "⏳" : cmd.status === "done" ? "✓" : cmd.status === "killed" ? "⊘" : "✗"}
        </span>
        <span className="mono clamp-1 rz-node-cmd-text">{cmd.command}</span>
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

function NodeCard({ n }: { n: LiveNodeInfo }) {
  const kick = useNodes((s) => s.kick);
  const rename = useNodes((s) => s.rename);
  const setMode = useNodes((s) => s.setMode);
  const killCommand = useNodes((s) => s.killCommand);
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(n.name);

  const cmds = [...n.runningCommands, ...n.recentCommands.slice().reverse()].slice(0, 8);
  const maxRank = ALL_MODES.indexOf(n.maxMode ?? "full");
  const allowedModes = ALL_MODES.slice(0, (maxRank >= 0 ? maxRank : ALL_MODES.length - 1) + 1);

  const saveRename = async () => {
    const trimmed = draftName.trim();
    setEditingName(false);
    if (!trimmed || trimmed === n.name) return;
    try {
      await rename(n.id, trimmed);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div className="rz-node-card online">
      <div className="rz-node-card-head">
        <span className="rz-node-dot on" />
        <div className="rz-node-card-title">
          {editingName ? (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <TextField
                size="small"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void saveRename();
                  if (e.key === "Escape") setEditingName(false);
                }}
                autoFocus
                sx={{ "& input": { py: 0.4, fontSize: 13 } }}
              />
              <IconButton size="small" onClick={() => void saveRename()}><Check size={14} /></IconButton>
              <IconButton size="small" onClick={() => setEditingName(false)}><X size={14} /></IconButton>
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <strong>{n.name}</strong>
              <Tooltip title="修改节点名称（下次重连本会话自动沿用）">
                <IconButton size="small" onClick={() => { setDraftName(n.name); setEditingName(true); }}>
                  <Pencil size={12} />
                </IconButton>
              </Tooltip>
            </div>
          )}
          <span className="dim">{n.os || n.platform || "未知系统"}</span>
        </div>
        <div className="rz-node-card-actions" style={{ gap: 6 }}>
          <Select
            size="small"
            value={n.mode}
            onChange={(e) => void setMode(n.id, e.target.value as NodeMode).catch((err) => toast((err as Error).message, "error"))}
            sx={{ fontSize: 12, "& .MuiSelect-select": { py: 0.4, px: 1.2 } }}
          >
            {allowedModes.map((m) => (
              <MenuItem key={m} value={m} sx={{ fontSize: 12.5 }}>{NODE_MODE_LABEL[m]}</MenuItem>
            ))}
          </Select>
          <Tooltip title="断开连接（立刻销毁本次临时连接）">
            <IconButton size="small" className="rz-danger" onClick={() => void kick(n.id)}>
              <Power size={15} />
            </IconButton>
          </Tooltip>
        </div>
      </div>

      <div className="rz-node-meta">
        {n.note && <div>备注：{n.note}</div>}
        <div className="mono dim">
          {n.dirs.length
            ? `允许目录：${n.dirs.join("  |  ")}`
            : "⚠ 尚未在电脑端选择允许目录（请在电脑端按 e 添加目录）"}
        </div>
      </div>

      {cmds.length > 0 && (
        <div className="rz-node-cmds">
          {cmds.map((c) => <CmdItem key={c.id} cmd={c} onKill={() => void killCommand(n.id, c.id)} />)}
        </div>
      )}
    </div>
  );
}

export function NodesDialog() {
  const sessionId = useNodes((s) => s.dialogSessionId);
  const openForSession = useNodes((s) => s.openForSession);
  const nodes = useNodes((s) => s.nodes);
  const approvals = useNodes((s) => s.approvals);
  const pairings = useNodes((s) => s.pairings);
  const downloadToken = useNodes((s) => s.downloadToken);
  const downloadTokenPinned = useNodes((s) => s.downloadTokenPinned);
  const rotateDownloadToken = useNodes((s) => s.rotateDownloadToken);
  const createPairing = useNodes((s) => s.createPairing);
  const resolveApproval = useNodes((s) => s.resolveApproval);
  const summary = useApp((s) => (sessionId ? s.sessions[sessionId] : undefined));

  const [mode, setMode] = useState<NodeMode>("auto");
  const [busy, setBusy] = useState(false);

  if (!sessionId) return null;

  const myNodes = nodes.filter((n) => n.sessionId === sessionId);
  const myApprovals = approvals.filter((a) => a.sessionId === sessionId);
  const myPairings = pairings.filter((p) => p.sessionId === sessionId);
  const sessionTitle = summary ? getSessionTitle(summary) : sessionId.slice(0, 8);

  const origin = typeof window !== "undefined" ? window.location.origin : "https://your-server";
  const bootstrapUrl = downloadToken
    ? `${origin}/api/nodes/bootstrap.py?token=${encodeURIComponent(downloadToken)}`
    : `${origin}/api/nodes/bootstrap.py?token=<loading>`;
  const oneLiner = `python -c "import urllib.request as u; exec(u.urlopen(u.Request('${bootstrapUrl}', headers={'User-Agent': 'riesz-node/1'})).read())"`;

  const onCreatePair = async () => {
    setBusy(true);
    try {
      await createPairing(sessionId, mode);
      toast("已为本会话生成 5 分钟有效的一次性配对码", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const copyCmd = (cmd: string) => {
    void copyText(cmd);
    toast("已复制", "success");
  };

  return (
    <Dialog open onClose={() => openForSession(null)} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, pr: 6 }}>
        <Laptop size={18} />
        <span>本地电脑 · {sessionTitle}</span>
        <span className="dim mono" style={{ fontSize: 12 }}>({sessionId.slice(0, 8)})</span>
        <IconButton size="small" onClick={() => openForSession(null)} sx={{ position: "absolute", right: 12, top: 12 }}>
          <X size={18} />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers className="rz-nodes-dialog">
        {myApprovals.length > 0 && (
          <section className="rz-nodes-section">
            <h4>待审批指令（{myApprovals.length}）</h4>
            {myApprovals.map((ap) => (
              <div key={ap.id} className="rz-node-approval-card">
                <div className="rz-node-approval-head">
                  <strong>[{ap.nodeName}]</strong>
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
          <h4>本会话已连接的电脑（{myNodes.length}）</h4>
          {myNodes.length === 0 ? (
            <div className="dim" style={{ fontSize: 13 }}>
              当前会话还没有连接本地电脑。在下方生成一次性配对码并在电脑端运行命令即可直连到此会话（仅本会话可见，断开即彻底失效）。
            </div>
          ) : (
            <div className="rz-nodes-list">
              {myNodes.map((n) => <NodeCard key={n.id} n={n} />)}
            </div>
          )}
        </section>

        <section className="rz-nodes-section">
          <h4>接入本地电脑到此会话</h4>
          <p className="dim" style={{ margin: "0 0 10px", fontSize: 12.5 }}>
            每次连接都是独立的一次性加密通道（不保存私钥、服务端不记录设备，断开即销毁）；仅本会话能看到并调用。同一电脑再次绑定本会话时，会自动恢复上次授权的目录与自定义名称。
          </p>
          <div className="rz-nodes-pair-form">
            <Select
              size="small"
              value={mode}
              onChange={(e) => setMode(e.target.value as NodeMode)}
              sx={{ minWidth: 160, fontSize: 13 }}
            >
              {ALL_MODES.map((m) => (
                <MenuItem key={m} value={m} sx={{ fontSize: 13 }}>初始权限：{NODE_MODE_LABEL[m]}</MenuItem>
              ))}
            </Select>
            <Button variant="contained" size="small" startIcon={<Plus size={15} />} disabled={busy} onClick={onCreatePair}>
              生成一次性配对码
            </Button>
          </div>

          {myPairings.map((p) => {
            const cmd = `${oneLiner} -- --code ${p.code}`;
            return (
              <div key={p.code} className="rz-node-pair-box">
                <div className="rz-node-pair-code">
                  <span className="mono">{p.code}</span>
                  <Tooltip title="只复制配对码">
                    <IconButton size="small" onClick={() => copyCmd(p.code)}><Copy size={13} /></IconButton>
                  </Tooltip>
                  <span className="dim" style={{ marginLeft: "auto" }}>
                    直连本会话 · {NODE_MODE_LABEL[p.mode]} · 一次性 · 5 分钟有效
                  </span>
                </div>
                <div className="rz-node-pair-cmd">
                  <code className="mono">{cmd}</code>
                  <Tooltip title="复制含配对码的一键命令">
                    <span>
                      <IconButton size="small" disabled={!downloadToken} onClick={() => copyCmd(cmd)}>
                        <Copy size={14} />
                      </IconButton>
                    </span>
                  </Tooltip>
                </div>
              </div>
            );
          })}

          <div className="rz-node-pair-box" style={{ marginTop: 12 }}>
            <div className="rz-node-pair-code">
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>通用一键启动命令（可存进快捷脚本，启动后输入配对码）</span>
              <span className="dim">需 Python 3.10+</span>
            </div>
            <div className="rz-node-pair-cmd">
              <code className="mono">{oneLiner}</code>
              <Tooltip title="复制通用一键启动命令">
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
        </section>
      </DialogContent>
    </Dialog>
  );
}
