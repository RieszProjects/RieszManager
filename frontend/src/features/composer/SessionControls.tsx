/** Model and permission-mode pickers shown in the composer toolbar. */
import { ListItemText, Menu, MenuItem } from "@mui/material";
import { Check, ChevronDown, Cpu, Shield } from "lucide-react";
import { useState } from "react";
import type { ModelInfo } from "@/api/types";
import { modelLabel, PERMISSION_MODES, permissionLabel } from "@/lib/format";

interface ModeProps {
  value: string | null | undefined;
  allowBypass: boolean;
  onChange: (mode: string) => void;
  disabled?: boolean;
}

export function ModePicker({ value, allowBypass, onChange, disabled }: ModeProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const modes = PERMISSION_MODES.filter((m) => allowBypass || m.value !== "bypassPermissions");
  const current = value ?? "default";
  return (
    <>
      <button
        type="button"
        className={`rz-chip mode-${current}`}
        disabled={disabled}
        onClick={(e) => setAnchor(e.currentTarget)}
        title="权限模式（Shift+Tab 切换）"
      >
        <Shield size={13} />
        <span>{permissionLabel(current)}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}>
        {modes.map((m) => (
          <MenuItem key={m.value} selected={m.value === current} onClick={() => { setAnchor(null); onChange(m.value); }}>
            <ListItemText primary={m.label} secondary={m.hint} slotProps={{ secondary: { sx: { fontSize: 12 } } }} />
            {m.value === current && <Check size={15} />}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

export function cycleMode(current: string | null | undefined, allowBypass: boolean): string {
  const order = ["default", "acceptEdits", "plan", "auto", ...(allowBypass ? ["bypassPermissions"] : [])];
  const idx = order.indexOf(current ?? "default");
  return order[(idx + 1) % order.length];
}

interface ModelProps {
  value: string | null | undefined;
  models: ModelInfo[];
  onChange: (model: string) => void;
  disabled?: boolean;
}

export function ModelPicker({ value, models, onChange, disabled }: ModelProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const isCurrent = (m: ModelInfo) => value === m.value || (m.resolvedModel && value === m.resolvedModel);
  return (
    <>
      <button type="button" className="rz-chip" disabled={disabled || !models.length} onClick={(e) => setAnchor(e.currentTarget)} title="模型">
        <Cpu size={13} />
        <span>{models.find((m) => m.value === value)?.displayName ?? modelLabel(value)}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}>
        {models.map((m) => (
          <MenuItem key={m.value} selected={Boolean(isCurrent(m))} onClick={() => { setAnchor(null); onChange(m.value); }}>
            <ListItemText primary={m.displayName} secondary={m.description} slotProps={{ secondary: { sx: { fontSize: 12 } } }} />
            {isCurrent(m) && <Check size={15} />}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}
