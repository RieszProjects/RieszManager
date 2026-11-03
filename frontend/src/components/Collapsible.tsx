import { ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";

interface Props {
  summary: ReactNode;
  children: ReactNode | (() => ReactNode);
  defaultOpen?: boolean;
  className?: string;
  disabled?: boolean;
}

/** Disclosure row whose body is only rendered once opened. */
export function Collapsible({ summary, children, defaultOpen = false, className, disabled }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`rz-collapsible${open ? " open" : ""}${className ? ` ${className}` : ""}`}>
      <button type="button" className="rz-collapsible-head" onClick={() => !disabled && setOpen(!open)} disabled={disabled}>
        {!disabled && <ChevronRight size={14} className="rz-chevron" />}
        <span className="rz-collapsible-summary">{summary}</span>
      </button>
      {open && <div className="rz-collapsible-body">{typeof children === "function" ? children() : children}</div>}
    </div>
  );
}
