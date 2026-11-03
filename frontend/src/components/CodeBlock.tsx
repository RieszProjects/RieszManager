import hljs from "highlight.js/lib/common";
import { memo, useMemo } from "react";

const HIGHLIGHT_LIMIT = 250_000;

interface Props {
  code: string;
  language?: string | null;
  lineNumbers?: boolean;
  startLine?: number;
  maxHeight?: number | string;
  wrap?: boolean;
}

function highlight(code: string, language?: string | null): string | null {
  if (code.length > HIGHLIGHT_LIMIT) return null;
  try {
    if (language && language !== "plaintext" && hljs.getLanguage(language)) {
      return hljs.highlight(code, { language, ignoreIllegals: true }).value;
    }
  } catch {
    return null;
  }
  return null;
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function CodeBlockImpl({ code, language, lineNumbers, startLine = 1, maxHeight, wrap }: Props) {
  const html = useMemo(() => highlight(code, language) ?? escapeHtml(code), [code, language]);
  const lines = useMemo(() => (lineNumbers ? html.split("\n") : null), [html, lineNumbers]);
  return (
    <div className={`rz-code${wrap ? " rz-code-wrap" : ""}`} style={{ maxHeight }}>
      {lines ? (
        <table>
          <tbody>
            {lines.map((line, i) => (
              <tr key={i}>
                <td className="rz-ln">{startLine + i}</td>
                <td className="hljs" dangerouslySetInnerHTML={{ __html: line || " " }} />
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <pre className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
      )}
    </div>
  );
}

export const CodeBlock = memo(CodeBlockImpl);
