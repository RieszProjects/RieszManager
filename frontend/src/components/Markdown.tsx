import { memo, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { Check, Copy } from "lucide-react";
import { copyText } from "@/lib/clipboard";

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

const components: Components = {
  pre: Pre,
  a: ({ href, children }) => (
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
  return (
    <div className={`md${sans ? " md-sans" : ""}${className ? ` ${className}` : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }], rehypeKatex]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
