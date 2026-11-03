/** Inline prompts the agent is waiting on: tool permissions, questions and plan approval. */
import { Button, TextField } from "@mui/material";
import { Check, ShieldQuestion, X } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import type { PermissionRequest, PermissionSuggestion } from "@/api/types";
import { CodeBlock } from "@/components/CodeBlock";
import { DiffView } from "@/components/DiffView";
import { Markdown } from "@/components/Markdown";
import { languageFor } from "@/lib/language";
import { toast } from "@/store/ui";
import { useSessionContext } from "./SessionContext";
import { toolMeta } from "./tools/toolMeta";

const str = (v: unknown): string => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v, null, 2));

async function answer(sessionId: string, requestId: string, body: Record<string, unknown>) {
  try {
    await api(`sessions/${sessionId}/permissions/${encodeURIComponent(requestId)}`, { body });
  } catch (err) {
    toast((err as Error).message, "error");
  }
}

function suggestionLabel(s: PermissionSuggestion): string | null {
  if (s.type === "setMode" && s.mode === "acceptEdits") return "允许，并自动接受本会话的编辑";
  if (s.type === "setMode" && s.mode) return `允许，并切换到 ${s.mode}`;
  if (s.type === "addRules" && s.rules?.length) {
    const r = s.rules[0];
    return `允许，且不再询问 ${r.toolName}${r.ruleContent ? `(${r.ruleContent})` : ""}`;
  }
  if (s.type === "addDirectories" && s.directories?.length) return `允许，并信任目录 ${s.directories[0]}`;
  return null;
}

function Preview({ req }: { req: PermissionRequest }) {
  const input = req.input ?? {};
  switch (req.toolName) {
    case "Bash":
      return <CodeBlock code={str(input.command)} language="bash" wrap maxHeight={220} />;
    case "Edit":
      return <DiffView oldText={str(input.old_string)} newText={str(input.new_string)} maxHeight={300} />;
    case "Write":
      return <CodeBlock code={str(input.content)} language={languageFor(str(input.file_path))} lineNumbers maxHeight={300} />;
    default:
      return Object.keys(input).length ? <CodeBlock code={JSON.stringify(input, null, 2)} language="json" wrap maxHeight={220} /> : null;
  }
}

function ToolPermission({ req }: { req: PermissionRequest }) {
  const { sessionId, cwd } = useSessionContext();
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const meta = toolMeta(req.toolName, req.input ?? {}, cwd);
  const suggestions = useMemo(
    () => (req.suppressAlwaysAllow ? [] : req.suggestions.map((s) => ({ s, label: suggestionLabel(s) })).filter((x) => x.label)),
    [req],
  );
  const run = async (body: Record<string, unknown>) => {
    setBusy(true);
    await answer(sessionId, req.requestId, body);
    setBusy(false);
  };
  return (
    <div className="rz-prompt-card">
      <div className="rz-prompt-head">
        <ShieldQuestion size={16} />
        <span>{req.title || `允许 ${meta.label}？`}</span>
      </div>
      {meta.summary && <div className="rz-prompt-target mono">{meta.summary}</div>}
      {req.description && req.description !== meta.summary && <div className="rz-prompt-desc">{req.description}</div>}
      {req.reason && <div className="rz-prompt-reason">{req.reason.replace(/\u001b\[[0-9;]*m/g, "")}</div>}
      <Preview req={req} />
      {showFeedback && (
        <TextField
          autoFocus
          fullWidth
          multiline
          maxRows={4}
          placeholder="告诉 Claude 应该怎么做（可选）"
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          sx={{ mt: 1 }}
        />
      )}
      <div className="rz-prompt-actions">
        <Button variant="contained" startIcon={<Check size={15} />} disabled={busy} autoFocus={!req.defaultToNo}
          onClick={() => run({ behavior: "allow" })}>
          允许
        </Button>
        {suggestions.map(({ s, label }, i) => (
          <Button key={i} variant="outlined" disabled={busy} onClick={() => run({ behavior: "allow", updatedPermissions: [s] })}>
            {label}
          </Button>
        ))}
        <Button
          variant="outlined"
          color="error"
          startIcon={<X size={15} />}
          disabled={busy}
          onClick={() => (showFeedback ? run({ behavior: "deny", message: feedback || undefined }) : setShowFeedback(true))}
        >
          {showFeedback ? "拒绝并反馈" : "拒绝"}
        </Button>
      </div>
    </div>
  );
}

interface Question {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options: { label: string; description?: string }[];
}

function AskQuestion({ req }: { req: PermissionRequest }) {
  const { sessionId } = useSessionContext();
  const questions = (req.input.questions as Question[] | undefined) ?? [];
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const toggle = (q: Question, label: string) =>
    setPicked((p) => {
      const cur = p[q.question] ?? [];
      if (q.multiSelect) return { ...p, [q.question]: cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label] };
      return { ...p, [q.question]: [label] };
    });
  const ready = questions.every((q) => (picked[q.question]?.length ?? 0) > 0 || (other[q.question] ?? "").trim());
  const submit = async () => {
    const answers: Record<string, string> = {};
    for (const q of questions) {
      const parts = [...(picked[q.question] ?? [])];
      if ((other[q.question] ?? "").trim()) parts.push(other[q.question].trim());
      answers[q.question] = parts.join(", ");
    }
    setBusy(true);
    await answer(sessionId, req.requestId, { behavior: "allow", updatedInput: { ...req.input, answers } });
    setBusy(false);
  };
  return (
    <div className="rz-prompt-card ask">
      {questions.map((q) => (
        <div key={q.question} className="rz-question">
          {q.header && <div className="rz-question-header">{q.header}</div>}
          <div className="rz-question-text">{q.question}</div>
          <div className="rz-options">
            {q.options.map((o) => {
              const on = picked[q.question]?.includes(o.label);
              return (
                <button key={o.label} type="button" className={`rz-option${on ? " on" : ""}`} onClick={() => toggle(q, o.label)}>
                  <span className="rz-option-label">{o.label}</span>
                  {o.description && <span className="rz-option-desc">{o.description}</span>}
                </button>
              );
            })}
          </div>
          <TextField
            fullWidth
            size="small"
            placeholder="其他（自行输入）"
            value={other[q.question] ?? ""}
            onChange={(e) => setOther((s) => ({ ...s, [q.question]: e.target.value }))}
          />
        </div>
      ))}
      <div className="rz-prompt-actions">
        <Button variant="contained" disabled={!ready || busy} onClick={submit}>提交回答</Button>
        <Button variant="outlined" color="error" disabled={busy}
          onClick={() => answer(sessionId, req.requestId, { behavior: "deny", message: "The user declined to answer." })}>
          跳过
        </Button>
      </div>
    </div>
  );
}

function PlanApproval({ req }: { req: PermissionRequest }) {
  const { sessionId } = useSessionContext();
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (body: Record<string, unknown>) => {
    setBusy(true);
    await answer(sessionId, req.requestId, body);
    setBusy(false);
  };
  return (
    <div className="rz-prompt-card plan">
      <div className="rz-prompt-head"><ShieldQuestion size={16} /> 是否按此计划执行？</div>
      <div className="rz-plan-body"><Markdown text={str(req.input.plan)} sans /></div>
      <TextField fullWidth multiline maxRows={4} placeholder="修改意见（继续规划时发送）" value={feedback}
        onChange={(e) => setFeedback(e.target.value)} sx={{ mt: 1 }} />
      <div className="rz-prompt-actions">
        <Button variant="contained" disabled={busy}
          onClick={() => run({ behavior: "allow", updatedPermissions: [{ type: "setMode", mode: "acceptEdits", destination: "session" }] })}>
          执行，并自动接受编辑
        </Button>
        <Button variant="outlined" disabled={busy} onClick={() => run({ behavior: "allow" })}>执行，逐项确认</Button>
        <Button variant="outlined" color="error" disabled={busy}
          onClick={() => run({ behavior: "deny", message: feedback || "The user wants to keep planning." })}>
          继续规划
        </Button>
      </div>
    </div>
  );
}

export function PendingPrompts({ requests }: { requests: PermissionRequest[] }) {
  if (!requests.length) return null;
  const req = requests[0];
  if (req.toolName === "AskUserQuestion") return <AskQuestion key={req.requestId} req={req} />;
  if (req.toolName === "ExitPlanMode") return <PlanApproval key={req.requestId} req={req} />;
  return <ToolPermission key={req.requestId} req={req} />;
}
