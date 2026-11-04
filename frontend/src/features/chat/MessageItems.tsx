/** Renderers for each transcript item kind. */
import { Brain, ChevronRight, CircleSlash, Command, Copy, Radio, ScrollText, SquareTerminal, Users } from "lucide-react";
import { memo, useState } from "react";
import { imageUrl } from "@/api/client";
import type { AssistantBlock, AssistantItem, BashItem, CommandItem, CompactItem, Item, NoticeItem, OutputItem, TaskItem, UserItem } from "@/api/types";
import { Collapsible } from "@/components/Collapsible";
import { Markdown } from "@/components/Markdown";
import { copyText } from "@/lib/clipboard";
import { clockTime, duration, modelLabel } from "@/lib/format";
import { toast, useUi } from "@/store/ui";
import { useSessionContext } from "./SessionContext";
import { ToolBlock } from "./tools/ToolBlock";

const MENTION_RE = /(^|\s)(@[^\s]+)/g;

function UserText({ text }: { text: string }) {
  const parts: (string | { mention: string })[] = [];
  let last = 0;
  for (const m of text.matchAll(MENTION_RE)) {
    const start = (m.index ?? 0) + m[1].length;
    if (start > last) parts.push(text.slice(last, start));
    parts.push({ mention: m[2] });
    last = start + m[2].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return (
    <div className="rz-user-text">
      {parts.map((p, i) => (typeof p === "string" ? p : <span key={i} className="rz-mention">{p.mention.split("/").pop()}</span>))}
    </div>
  );
}

const UserBubble = memo(function UserBubble({ item }: { item: UserItem }) {
  const { sessionId } = useSessionContext();
  const [expanded, setExpanded] = useState(false);
  const openGallery = useUi((s) => s.openGallery);
  const long = item.text.length > 1600;
  const text = long && !expanded ? `${item.text.slice(0, 1600)}…` : item.text;
  if (item.origin === "peer") {
    return (
      <div className="rz-peer">
        <div className="rz-peer-head"><Users size={13} /> 来自会话 {item.peerName || "其他会话"}</div>
        <Markdown text={item.text} sans />
      </div>
    );
  }
  return (
    <div className="rz-user">
      <div className="rz-user-bubble">
        {item.images.length > 0 && (
          <div className="rz-user-images">
            {item.images.map((img, i) => (
              <button
                key={img.ref}
                type="button"
                className="rz-image-thumb"
                aria-label="查看大图"
                onClick={() => openGallery({ urls: item.images.map((m) => imageUrl(sessionId, m.ref)), index: i })}
              >
                <img src={imageUrl(sessionId, img.ref)} alt="" loading="lazy" />
              </button>
            ))}
          </div>
        )}
        {text && <UserText text={text} />}
        {long && (
          <button type="button" className="rz-linklike" onClick={() => setExpanded(!expanded)}>
            {expanded ? "收起" : "展开全部"}
          </button>
        )}
      </div>
      <div className="rz-user-meta">
        {item.queued && <span className="rz-tag">运行中插入</span>}
        <span>{clockTime(item.ts)}</span>
        <button type="button" aria-label="复制" onClick={async () => (await copyText(item.text)) && toast("已复制", "success")}>
          <Copy size={12} />
        </button>
      </div>
    </div>
  );
});

function ThinkingBlock({ block }: { block: Extract<AssistantBlock, { type: "thinking" }> }) {
  const label = block.durationMs && block.durationMs >= 1000 ? `已思考 ${duration(block.durationMs)}` : "已思考";
  if (block.redacted || !block.text.trim()) {
    return <div className="rz-thinking muted"><Brain size={14} /> {label}</div>;
  }
  return (
    <Collapsible className="rz-thinking" summary={<><Brain size={14} /> {label}</>}>
      <div className="rz-thinking-body"><Markdown text={block.text} sans /></div>
    </Collapsible>
  );
}

const AssistantMessage = memo(function AssistantMessage({ item }: { item: AssistantItem }) {
  if (item.synthetic) {
    const text = item.blocks.map((b) => (b.type === "text" ? b.text : "")).join("\n");
    if (!text.trim()) return null;
    return <div className={`rz-synthetic${item.isError ? " error" : ""}`}><Markdown text={text} sans /></div>;
  }
  return (
    <div className="rz-assistant">
      {item.blocks.map((block, i) => {
        if (block.type === "text") return <Markdown key={i} text={block.text} />;
        if (block.type === "thinking") return <ThinkingBlock key={i} block={block} />;
        return <ToolBlock key={block.id ?? i} tool={block} />;
      })}
    </div>
  );
});

function CommandLine({ item }: { item: CommandItem }) {
  return (
    <div className="rz-command">
      <Command size={13} />
      <span className="mono">{item.name}{item.args ? ` ${item.args}` : ""}</span>
    </div>
  );
}

function OutputBlock({ item }: { item: OutputItem }) {
  if (!item.text.trim()) return null;
  const looksMarkdown = /(^|\n)(#{1,4} |\|.*\||[-*] )/.test(item.text);
  return (
    <div className={`rz-output${item.isError ? " error" : ""}`}>
      {looksMarkdown ? <Markdown text={item.text} sans /> : <pre>{stripAnsi(item.text)}</pre>}
    </div>
  );
}

function stripAnsi(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
}

function BashBlock({ item }: { item: BashItem }) {
  return (
    <div className="rz-bashmode">
      <div className="rz-bashmode-cmd"><SquareTerminal size={13} /> <span className="mono">! {item.command}</span></div>
      {(item.stdout || item.stderr) && <pre className={item.stderr && !item.stdout ? "error" : ""}>{stripAnsi(item.stdout || item.stderr)}</pre>}
    </div>
  );
}

function NoticeLine({ item }: { item: NoticeItem }) {
  return <div className={`rz-notice level-${item.level}`}>{item.text}</div>;
}

function CompactDivider({ item }: { item: CompactItem }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rz-compact">
      <button type="button" onClick={() => item.summary && setOpen(!open)} disabled={!item.summary}>
        <ScrollText size={13} />
        <span>{item.summary ? "上下文摘要" : "对话已压缩"}</span>
        {item.preTokens ? <span className="dim">· 压缩前 {Math.round(item.preTokens / 1000)}k tokens</span> : null}
        {item.summary && <ChevronRight size={13} className={open ? "rot" : ""} />}
      </button>
      {open && item.summary && <div className="rz-compact-body"><Markdown text={item.summary} sans /></div>}
    </div>
  );
}

function TaskNotice({ item }: { item: TaskItem }) {
  return (
    <div className={`rz-task status-${item.status}`}>
      <Radio size={13} />
      <span>{item.summary || `后台任务 ${item.taskId} ${item.status}`}</span>
    </div>
  );
}

export const ItemView = memo(function ItemView({ item }: { item: Item }) {
  switch (item.kind) {
    case "user":
      return <UserBubble item={item} />;
    case "assistant":
      return <AssistantMessage item={item} />;
    case "command":
      return <CommandLine item={item} />;
    case "output":
      return <OutputBlock item={item} />;
    case "bash":
      return <BashBlock item={item} />;
    case "notice":
      return <NoticeLine item={item} />;
    case "compact":
      return <CompactDivider item={item} />;
    case "interrupted":
      return <div className="rz-interrupted"><CircleSlash size={13} /> 已中断</div>;
    case "task":
      return <TaskNotice item={item} />;
    default:
      return null;
  }
});

export function modelBadge(model: string | null | undefined): string {
  return modelLabel(model);
}
