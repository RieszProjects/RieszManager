/** Inline approval prompt rendered inside a session when its bound local computer node awaits confirmation. */
import { Button } from "@mui/material";
import { Check, Laptop, X } from "lucide-react";
import { useState } from "react";
import { toast } from "@/store/ui";
import { useNodes } from "./store";

const RISK_LABEL: Record<string, string> = { low: "低风险", medium: "中风险", high: "高风险" };

export function NodeApprovalBanner({ sessionId }: { sessionId: string }) {
  const approvals = useNodes((s) => s.approvals);
  const resolveApproval = useNodes((s) => s.resolveApproval);
  const [busy, setBusy] = useState(false);

  const mine = approvals.filter((a) => a.sessionId === sessionId);
  if (!mine.length) return null;
  const ap = mine[0];

  const decide = async (allow: boolean) => {
    setBusy(true);
    try {
      await resolveApproval(ap.id, allow);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rz-prompt-card rz-node-prompt">
      <div className="rz-prompt-head">
        <Laptop size={16} />
        <span>本机 [{ap.nodeName}] 请求{ap.action === "push" ? "接收文件" : ap.action === "pull" ? "拉取文件" : "执行命令"}</span>
        <span className={`rz-tag risk-${ap.risk}`} style={{ marginLeft: "auto" }}>{RISK_LABEL[ap.risk] ?? ap.risk}</span>
      </div>
      <pre className="mono rz-node-approval-cmd">{ap.command}</pre>
      {ap.cwd && <div className="rz-prompt-target mono">目录：{ap.cwd}</div>}
      {ap.reason && <div className="rz-prompt-reason">{ap.reason}（也可在电脑端按 y / n 审批）</div>}
      <div className="rz-prompt-actions">
        <Button variant="contained" size="small" startIcon={<Check size={14} />} disabled={busy} onClick={() => void decide(true)}>
          允许执行
        </Button>
        <Button variant="outlined" size="small" color="error" startIcon={<X size={14} />} disabled={busy} onClick={() => void decide(false)}>
          拒绝
        </Button>
      </div>
    </div>
  );
}
