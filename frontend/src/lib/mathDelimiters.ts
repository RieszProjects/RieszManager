/**
 * Prepare Markdown for remark-math, which knows `$…$` and `$$…$$` only and takes every pair of dollars for math.
 *
 * - `\(…\)` becomes `$…$` and `\[…\]` becomes a display formula.
 * - A `$` that does not open or close a formula is escaped (`\$`), so that "from $5 to $10" stays text.
 *   A pair counts as a formula by Pandoc's rule: no space right after the opening `$`, no space right before
 *   the closing one, no digit right after it, and no other `$` or blank line in between.
 * - `$$…$$` alone on a line becomes a display formula (remark-math shows it inline otherwise).
 *
 * Code (fenced blocks and `code spans`) is left as it is.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** Split into [text, code, text, code, …]: fenced blocks and code spans are the odd entries. */
function splitCode(source: string): string[] {
  // Character ranges of the fenced blocks (an unclosed one runs to the end, as while a reply is streaming).
  const ranges: [number, number][] = [];
  let offset = 0;
  let fence: { start: number; close: RegExp } | null = null;
  for (const line of source.split("\n")) {
    const end = offset + line.length;
    if (fence) {
      if (fence.close.test(line)) {
        ranges.push([fence.start, end]);
        fence = null;
      }
    } else {
      const open = FENCE.exec(line);
      if (open) fence = { start: offset, close: new RegExp(`^ {0,3}${open[1][0]}{${open[1].length},}[ \\t]*$`) };
    }
    offset = end + 1;
  }
  if (fence) ranges.push([fence.start, source.length]);

  const parts: string[] = [];
  let text = "";
  // Code spans in the text between the blocks: a run of backticks up to the next run of the same length.
  const spans = (chunk: string) => {
    const re = /(?<!`)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(chunk))) {
      parts.push(text + chunk.slice(last, m.index), m[0]);
      text = "";
      last = m.index + m[0].length;
    }
    text += chunk.slice(last);
  };
  let at = 0;
  for (const [from, to] of ranges) {
    spans(source.slice(at, from));
    parts.push(text, source.slice(from, to));
    text = "";
    at = to;
  }
  spans(source.slice(at));
  parts.push(text);
  return parts;
}

const isSpace = (ch: string | undefined) => ch === undefined || /\s/.test(ch);

/** Escape the dollars of `text` (no code in it) that are not formula delimiters. */
function dollars(text: string): string {
  let out = "";
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];
    if (ch === "\\") {
      out += text.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (ch !== "$") {
      out += ch;
      i++;
      continue;
    }
    // A run of two or more dollars is a delimiter of its own (`$$…$$`): leave the whole run.
    let run = 1;
    while (text[i + run] === "$") run++;
    if (run > 1) {
      out += text.slice(i, i + run);
      i += run;
      continue;
    }
    // A single dollar: does it open a formula?
    let close = -1;
    if (!isSpace(text[i + 1])) {
      for (let j = i + 1; j < n; j++) {
        const c = text[j];
        if (c === "\\") {
          j++;
          continue;
        }
        if (c === "\n" && /^[ \t]*\n/.test(text.slice(j + 1))) break;   // a blank line ends the paragraph
        if (c !== "$") continue;
        const single = text[j + 1] !== "$";
        if (single && j > i + 1 && !isSpace(text[j - 1]) && !/\d/.test(text[j + 1] ?? "")) close = j;
        break;   // the first dollar decides: it closes the formula, or there is none
      }
    }
    if (close < 0) {
      out += "\\$";
      i++;
    } else {
      out += text.slice(i, close + 1);
      i = close + 1;
    }
  }
  return out;
}

/** `\(…\)` and `\[…\]` to dollars, and `$$…$$` alone on a line to a display formula. */
function brackets(text: string): string {
  return text
    .replace(/(^|[^\\])\\\[([\s\S]+?)\\\]/g, (all, before: string, body: string) =>
      /\n[ \t]*\n/.test(body) ? all : `${before}\n$$\n${body.trim()}\n$$\n`)
    .replace(/(^|[^\\])\\\(([^\n]*?(?:\n[^\n]+?)*?)\\\)/g, (all, before: string, body: string) =>
      body.trim() && !body.includes("$") ? `${before}$${body.trim()}$` : all)
    .replace(/^([ \t]*)\$\$(?!\$)([^\n$]+?)\$\$[ \t]*$/gm, (_all, indent: string, body: string) =>
      `${indent}$$\n${indent}${body.trim()}\n${indent}$$`);
}

export function prepareMath(source: string): string {
  if (!source.includes("$") && !source.includes("\\(") && !source.includes("\\[")) return source;
  const parts = splitCode(source);
  return parts.map((part, i) => (i % 2 ? part : dollars(brackets(part)))).join("");
}
