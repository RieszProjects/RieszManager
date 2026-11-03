/** Keyboard-navigable suggestion list shown above the composer (slash commands, @files). */
import { File, Folder, SquareSlash } from "lucide-react";
import { useEffect, useRef } from "react";

export interface Suggestion {
  key: string;
  label: string;
  hint?: string;
  description?: string;
  kind: "command" | "file" | "dir";
}

interface Props {
  items: Suggestion[];
  active: number;
  onPick: (s: Suggestion, index: number) => void;
  onHover: (index: number) => void;
  title: string;
  empty?: string;
}

export function SuggestMenu({ items, active, onPick, onHover, title, empty }: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);
  return (
    <div className="rz-suggest" role="listbox">
      <div className="rz-suggest-title">{title}</div>
      <div className="rz-suggest-list" ref={listRef}>
        {items.length === 0 && <div className="rz-suggest-empty">{empty ?? "没有匹配项"}</div>}
        {items.map((s, i) => {
          const Icon = s.kind === "command" ? SquareSlash : s.kind === "dir" ? Folder : File;
          return (
            <button
              key={s.key}
              type="button"
              data-idx={i}
              role="option"
              aria-selected={i === active}
              className={`rz-suggest-item${i === active ? " active" : ""}`}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => onPick(s, i)}
              onMouseMove={() => i !== active && onHover(i)}
            >
              <Icon size={14} className="rz-suggest-icon" />
              <span className="rz-suggest-label mono">{s.label}</span>
              {s.hint && <span className="rz-suggest-hint mono">{s.hint}</span>}
              {s.description && <span className="rz-suggest-desc">{s.description}</span>}
            </button>
          );
        })}
      </div>
      <div className="rz-suggest-foot"><kbd>↑</kbd><kbd>↓</kbd> 选择 <kbd>Tab</kbd> 补全 <kbd>Enter</kbd> 确认 <kbd>Esc</kbd> 关闭</div>
    </div>
  );
}
