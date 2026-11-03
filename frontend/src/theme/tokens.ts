/**
 * Design tokens for the "contemporary dark academia" look: deep green-black
 * surfaces, parchment text, muted sage (gray-green) and muted amber (gray-orange).
 */
export const color = {
  bg: "#121513",
  bg1: "#181c19",
  bg2: "#1e231f",
  bg3: "#262c27",
  bg4: "#2f3630",
  line: "#323a33",
  lineSoft: "#262d27",
  text: "#e8e2d4",
  text2: "#b9b2a1",
  text3: "#878172",
  text4: "#5f5b51",
  sage: "#93a88c",
  sageStrong: "#adc2a5",
  sageDeep: "#58694f",
  sageMuted: "rgba(147, 168, 140, 0.14)",
  amber: "#c6966b",
  amberStrong: "#ddb089",
  amberDeep: "#8e6646",
  amberMuted: "rgba(198, 150, 107, 0.14)",
  danger: "#c46e62",
  dangerMuted: "rgba(196, 110, 98, 0.14)",
  warn: "#cfa865",
  info: "#8aa3b0",
} as const;

export const font = {
  display: '"Cormorant Garamond", "Noto Serif SC", "Songti SC", serif',
  serif: '"Source Serif 4 Variable", "Noto Serif SC", "Songti SC", "STSong", Georgia, serif',
  sans: '"Inter Variable", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", sans-serif',
  mono: '"JetBrains Mono Variable", "SFMono-Regular", Menlo, Consolas, "Noto Sans Mono CJK SC", monospace',
} as const;

export const radius = { sm: 6, md: 10, lg: 14, xl: 20 } as const;

export const layout = {
  sidebarWidth: 288,
  rightPanelWidth: 340,
  mobileBreakpoint: 900,
  wideBreakpoint: 1320,
  contentMaxWidth: 860,
} as const;
