import { CheckCircle2, ChevronRight, Circle, CircleDot, CircleSlash, LoaderCircle, TriangleAlert } from "lucide-react";
import { memo, useState, type ReactNode } from "react";
import { imageUrl } from "@/api/client";
import type { ToolBlock as ToolBlockType, ToolResult } from "@/api/types";
import { CodeBlock } from "@/components/CodeBlock";
import { DiffView, diffStats, type Hunk } from "@/components/DiffView";
import { Markdown } from "@/components/Markdown";
import { languageFor } from "@/lib/language";
import { useUi } from "@/store/ui";
import { useSessionContext } from "../SessionContext";
import { mcpParts, toolMeta } from "./toolMeta";

const OPEN_BY_DEFAULT = new Set(["Edit", "MultiEdit", "Write", "TodoWrite", "ExitPlanMode", "AskUserQuestion", "NotebookEdit"]);
const str = (v: unknown): string => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v, null, 2));

function ResultImages({ result }: { result: ToolResult }) {
  const { sessionId } = useSessionContext();
  const openGallery = useUi((s) => s.openGallery);
  if (!result.images?.length) return null;
  return (
    <div className="rz-tool-images">
      {result.images.map((img, i) => (
        <button
          key={img.ref}
          type="button"
          className="rz-image-thumb"
          aria-label="查看大图"
          onClick={() => openGallery({ urls: result.images.map((m) => imageUrl(sessionId, m.ref)), index: i })}
        >
          <img src={imageUrl(sessionId, img.ref)} alt="" loading="lazy" />
        </button>
      ))}
    </div>
  );
}

function Output({ text, error, max = 360 }: { text: string; error?: boolean; max?: number }) {
  if (!text.trim()) return <div className="rz-tool-empty">（无输出）</div>;
  return <pre className={`rz-tool-output${error ? " error" : ""}`} style={{ maxHeight: max }}>{text}</pre>;
}

function Truncated({ result }: { result: ToolResult | null }) {
  return result?.truncated ? <div className="rz-tool-note">输出过长，已截断</div> : null;
}

function BashBody({ input, result }: { input: Record<string, unknown>; result: ToolResult | null }) {
  const s = result?.structured as { stdout?: string; stderr?: string; interrupted?: boolean } | undefined;
  const stdout = s?.stdout ?? result?.text ?? "";
  const stderr = s?.stderr ?? "";
  return (
    <>
      <div className="rz-tool-cmd">
        <span className="rz-prompt">$</span>
        <pre>{str(input.command)}</pre>
      </div>
      {result && (
        <>
          {(stdout || !stderr) && <Output text={stdout} error={result.isError && !stderr} />}
          {stderr && <Output text={stderr} error />}
          {s?.interrupted && <div className="rz-tool-note">已中断</div>}
          <ResultImages result={result} />
        </>
      )}
    </>
  );
}

function EditBody({ name, input, result }: { name: string; input: Record<string, unknown>; result: ToolResult | null }) {
  const patch = (result?.structured as { structuredPatch?: Hunk[] } | undefined)?.structuredPatch;
  if (result?.isError) return <Output text={result.text} error />;
  if (patch && patch.length) return <DiffView hunks={patch} maxHeight={420} />;
  if (name === "MultiEdit" && Array.isArray(input.edits)) {
    return (
      <>
        {(input.edits as { old_string?: string; new_string?: string }[]).map((e, i) => (
          <DiffView key={i} oldText={e.old_string ?? ""} newText={e.new_string ?? ""} maxHeight={300} />
        ))}
      </>
    );
  }
  return <DiffView oldText={str(input.old_string)} newText={str(input.new_string)} maxHeight={420} />;
}

function WriteBody({ input, result }: { input: Record<string, unknown>; result: ToolResult | null }) {
  if (result?.isError) return <Output text={result.text} error />;
  const path = str(input.file_path);
  return <CodeBlock code={str(input.content)} language={languageFor(path)} lineNumbers maxHeight={420} />;
}

function TodoBody({ input }: { input: Record<string, unknown> }) {
  const todos = (input.todos as { content?: string; status?: string; activeForm?: string }[] | undefined) ?? [];
  return (
    <ul className="rz-todos">
      {todos.map((t, i) => (
        <li key={i} className={t.status}>
          {t.status === "completed" ? <CheckCircle2 size={15} /> : t.status === "in_progress" ? <CircleDot size={15} /> : <Circle size={15} />}
          <span>{t.status === "in_progress" ? t.activeForm || t.content : t.content}</span>
        </li>
      ))}
    </ul>
  );
}

function AskBody({ input, result }: { input: Record<string, unknown>; result: ToolResult | null }) {
  const questions = (input.questions as { question: string; options?: { label: string }[] }[] | undefined) ?? [];
  const answers = ((result?.structured as { answers?: Record<string, string> } | undefined)?.answers) ?? {};
  return (
    <div className="rz-ask-summary">
      {questions.map((q, i) => (
        <div key={i}>
          <div className="q">{q.question}</div>
          <div className="a">{answers[q.question] ? `→ ${answers[q.question]}` : result ? result.text.slice(0, 300) : "等待回答…"}</div>
        </div>
      ))}
    </div>
  );
}

function TaskBody({ input, result }: { input: Record<string, unknown>; result: ToolResult | null }) {
  const [showPrompt, setShowPrompt] = useState(false);
  return (
    <>
      <button type="button" className="rz-linklike" onClick={() => setShowPrompt(!showPrompt)}>
        {showPrompt ? "收起任务说明" : "查看任务说明"}
      </button>
      {showPrompt && <div className="rz-tool-quote"><Markdown text={str(input.prompt)} sans /></div>}
      {result && (result.isError ? <Output text={result.text} error /> : <div className="rz-tool-md"><Markdown text={result.text} sans /></div>)}
    </>
  );
}

function GenericBody({ input, result, markdown }: { input: Record<string, unknown>; result: ToolResult | null; markdown?: boolean }) {
  const hasInput = Object.keys(input ?? {}).length > 0;
  return (
    <>
      {hasInput && <CodeBlock code={JSON.stringify(input, null, 2)} language="json" maxHeight={240} wrap />}
      {result && (
        <>
          {markdown && !result.isError ? (
            <div className="rz-tool-md"><Markdown text={result.text} sans /></div>
          ) : (
            (result.text || !result.images?.length) && <Output text={result.text} error={result.isError} />
          )}
          <ResultImages result={result} />
        </>
      )}
    </>
  );
}

function body(tool: ToolBlockType): ReactNode {
  const { name, input, result } = tool;
  switch (name) {
    case "Bash":
      return <BashBody input={input} result={result} />;
    case "Edit":
    case "MultiEdit":
      return <EditBody name={name} input={input} result={result} />;
    case "Write":
      return <WriteBody input={input} result={result} />;
    case "TodoWrite":
      return <TodoBody input={input} />;
    case "AskUserQuestion":
      return <AskBody input={input} result={result} />;
    case "ExitPlanMode":
      return <div className="rz-tool-md"><Markdown text={str(input.plan)} sans /></div>;
    case "Task":
    case "Agent":
      return <TaskBody input={input} result={result} />;
    case "Read":
    case "Grep":
    case "Glob":
    case "WebSearch":
      return result ? <><Output text={result.text} error={result.isError} /><ResultImages result={result} /></> : null;
    case "WebFetch":
      return <GenericBody input={{ prompt: input.prompt }} result={result} markdown />;
    default:
      return <GenericBody input={input} result={result} />;
  }
}

function headExtra(tool: ToolBlockType): ReactNode {
  const { name, input, result } = tool;
  if ((name === "Edit" || name === "MultiEdit" || name === "Write") && !result?.isError) {
    const patch = (result?.structured as { structuredPatch?: Hunk[] } | undefined)?.structuredPatch;
    let stats: { add: number; del: number };
    if (name === "Write" && !(patch && patch.length)) stats = { add: str(input.content).split("\n").length, del: 0 };
    else stats = diffStats(patch, name === "Edit" ? str(input.old_string) : undefined, name === "Edit" ? str(input.new_string) : undefined);
    if (!stats.add && !stats.del) return null;
    return (
      <span className="rz-tool-stats">
        {stats.add > 0 && <span className="add">+{stats.add}</span>}
        {stats.del > 0 && <span className="del">−{stats.del}</span>}
      </span>
    );
  }
  if (name === "TodoWrite") {
    const todos = (input.todos as { status?: string }[] | undefined) ?? [];
    const done = todos.filter((t) => t.status === "completed").length;
    return <span className="rz-tool-stats">{done}/{todos.length}</span>;
  }
  return null;
}

function ToolBlockImpl({ tool }: { tool: ToolBlockType }) {
  const { cwd, pendingTools } = useSessionContext();
  const meta = toolMeta(tool.name, tool.input ?? {}, cwd);
  const [open, setOpen] = useState(OPEN_BY_DEFAULT.has(tool.name));
  const pending = !tool.result && pendingTools.has(tool.id);
  const terminated = !tool.result && !pending;
  const error = tool.result?.isError;
  const Icon = meta.icon;
  const mcp = mcpParts(tool.name);
  return (
    <div className={`rz-tool${error ? " error" : ""}${pending ? " pending" : ""}${terminated ? " terminated" : ""}${open ? " open" : ""}`}>
      <button type="button" className="rz-tool-head" onClick={() => setOpen(!open)}>
        <span className="rz-tool-icon">
          {pending ? <LoaderCircle size={15} className="spin" /> : terminated ? <CircleSlash size={15} /> : error ? <TriangleAlert size={15} /> : <Icon size={15} />}
        </span>
        <span className="rz-tool-label">{meta.label}</span>
        {mcp && <span className="rz-tool-server">{mcp.server}</span>}
        <span className="rz-tool-summary mono">{meta.summary}</span>
        {terminated ? <span className="rz-tool-terminated">已终止</span> : headExtra(tool)}
        <ChevronRight size={14} className="rz-chevron" />
      </button>
      {open && (
        <div className="rz-tool-body">
          {body(tool)}
          <Truncated result={tool.result} />
        </div>
      )}
      {!open && error && tool.result && <div className="rz-tool-error-line">{tool.result.text.split("\n")[0].slice(0, 200)}</div>}
    </div>
  );
}

export const ToolBlock = memo(ToolBlockImpl);
