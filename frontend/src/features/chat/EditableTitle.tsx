/** Session title in the header; click (or tap) to rename it in place. */
import { useEffect, useRef, useState } from "react";
import { useApp } from "@/store/app";
import { toast } from "@/store/ui";

export function EditableTitle({ sessionId, title }: { sessionId: string; title: string }) {
  const renameSession = useApp((s) => s.renameSession);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  const input = useRef<HTMLInputElement>(null);
  const saving = useRef(false);

  useEffect(() => {
    if (!editing) setValue(title);
  }, [title, editing]);

  useEffect(() => {
    if (editing) input.current?.select();
  }, [editing]);

  const finish = async (save: boolean) => {
    if (saving.current) return;
    const next = value.trim();
    if (!save || !next || next === title) {
      setEditing(false);
      setValue(title);
      return;
    }
    saving.current = true;
    try {
      await renameSession(sessionId, next);
    } catch (err) {
      toast((err as Error).message, "error");
      setValue(title);
    } finally {
      saving.current = false;
      setEditing(false);
    }
  };

  if (editing) {
    return (
      <input
        ref={input}
        className="rz-title-input"
        value={value}
        maxLength={200}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => void finish(true)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Enter") {
            e.preventDefault();
            void finish(true);
          } else if (e.key === "Escape") {
            e.preventDefault();
            void finish(false);
          }
        }}
      />
    );
  }

  return (
    <button type="button" className="rz-title-button clamp-1" title="点击重命名" onClick={() => setEditing(true)}>
      {title}
    </button>
  );
}
