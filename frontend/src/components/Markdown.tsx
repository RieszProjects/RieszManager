import { memo, useMemo, useState, type MouseEvent, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { Check, Copy } from "lucide-react";
import { api } from "@/api/client";
import { copyText } from "@/lib/clipboard";
import { toast, useUi, type FileTarget } from "@/store/ui";
import { prepareMath } from "@/lib/mathDelimiters";

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

function Pre({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const child = Array.isArray(children) ? children[0] : children;
  const className: string = (child as { props?: { className?: string } })?.props?.className ?? "";
  const lang = className.match(/language-([\w+-]+)/)?.[1];
  const onCopy = async () => {
    if (await copyText(textOf(children))) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }
  };
  return (
    <div className="rz-codeblock">
      <div className="rz-codeblock-head">
        <span>{lang ?? "text"}</span>
        <button type="button" onClick={onCopy} aria-label="复制代码">
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

/** Links to local files survive (react-markdown drops unknown protocols); FileLink opens them. */
function keepFileLinks(url: string): string {
  return url.startsWith("file://") ? url : defaultUrlTransform(url);
}

/** A link to a local file (file:///...) in a reply: opened in the web app's file viewer (runner: open_file). */
function FileLink({ href, children }: { href: string; children: ReactNode }) {
  const onClick = (e: MouseEvent) => {
    e.preventDefault();
    const route = useUi.getState().route;
    if (route.view !== "session") return;
    api<FileTarget>(`sessions/${route.sessionId}/open-file`, { body: { path: href } })
      .then((target) => useUi.getState().openFile(target))
      .catch((err: Error) => toast(err.message, "error"));
  };
  return (
    <a href={href} onClick={onClick} title={decodeURIComponent(href.replace(/^file:\/\//, ""))}>
      {children}
    </a>
  );
}

const components: Components = {
  pre: Pre,
  a: ({ href, children }) =>
    href?.startsWith("file://") ? (
      <FileLink href={href}>{children}</FileLink>
    ) : (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ),
};

interface Props {
  text: string;
  sans?: boolean;
  className?: string;
}

function MarkdownImpl({ text, sans, className }: Props) {
  // Formulas: `$…$`, `$$…$$`, `\(…\)` and `\[…\]`; dollars that are money stay text (see lib/mathDelimiters).
  const source = useMemo(() => prepareMath(text), [text]);
  return (
    <div className={`md${sans ? " md-sans" : ""}${className ? ` ${className}` : ""}`}>
      <ReactMarkdown
        urlTransform={keepFileLinks}
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }], rehypeKatex]}
        components={components}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
