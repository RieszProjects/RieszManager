/** Guess a highlight.js language name from a file path. */
const BY_EXT: Record<string, string> = {
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript",
  tsx: "typescript", json: "json", jsonl: "json", md: "markdown", html: "xml", htm: "xml", xml: "xml", svg: "xml",
  vue: "xml", css: "css", scss: "scss", less: "less", sh: "bash", bash: "bash", zsh: "bash", yml: "yaml",
  yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "nginx", rs: "rust", go: "go", java: "java",
  kt: "kotlin", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp", rb: "ruby", php: "php",
  sql: "sql", swift: "swift", lua: "lua", r: "r", tex: "latex", dockerfile: "dockerfile", makefile: "makefile",
  env: "bash", txt: "plaintext", diff: "diff", patch: "diff",
};

export function languageFor(path: string | null | undefined): string {
  if (!path) return "plaintext";
  const name = path.split("/").pop()?.toLowerCase() ?? "";
  if (name === "dockerfile") return "dockerfile";
  if (name === "makefile") return "makefile";
  if (name.startsWith(".env")) return "bash";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  return BY_EXT[ext] ?? "plaintext";
}
