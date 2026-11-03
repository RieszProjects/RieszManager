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

const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

/** Find the catalog entry for a model alias or full model id (aliases win over "default"). */
export function resolveModel(models: ModelInfo[], value: string | null | undefined): ModelInfo | undefined {
  if (!value) return undefined;
  return (
    models.find((m) => m.value === value) ??
    models.find((m) => m.value !== "default" && m.resolvedModel === value) ??
    models.find((m) => m.resolvedModel === value)
  );
}

function modelName(models: ModelInfo[], value: string | null | undefined): string {
  const option = resolveModel(models, value);
  if (!option) return modelLabel(value);
  if (option.value === "default") {
    const concrete = models.find((m) => m.value !== "default" && m.resolvedModel === option.resolvedModel);
    return concrete?.displayName ?? modelLabel(option.resolvedModel);
  }
  return option.displayName;
}

interface ModelProps {
  value: string | null | undefined;
  effort: string | null | undefined;
  models: ModelInfo[];
  onChange: (model: string) => void;
  onEffort: (effort: string) => void;
  disabled?: boolean;
}

export function ModelPicker({ value, effort, models, onChange, onEffort, disabled }: ModelProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const current = resolveModel(models, value);
  const levels = current?.supportedEffortLevels?.length ? current.supportedEffortLevels : EFFORT_LEVELS;
  const name = modelName(models, value);
  return (
    <>
      <button type="button" className="rz-chip" disabled={disabled || !models.length} onClick={(e) => setAnchor(e.currentTarget)} title="模型与推理强度">
        <Cpu size={13} />
        <span>{effort ? `${name} · ${effort}` : name}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}>
        {models.map((m) => (
          <MenuItem key={m.value} selected={m.value === current?.value} onClick={() => { setAnchor(null); onChange(m.value); }}>
            <ListItemText primary={m.displayName} secondary={m.description} slotProps={{ secondary: { sx: { fontSize: 12 } } }} />
            {m.value === current?.value && <Check size={15} />}
          </MenuItem>
        ))}
        <div className="rz-effort-row" onClick={(e) => e.stopPropagation()}>
          <span className="rz-effort-label">推理强度</span>
          {levels.map((level) => (
            <button key={level} type="button" className={`rz-effort${level === effort ? " on" : ""}`}
              onClick={() => { setAnchor(null); onEffort(level); }}>
              {level}
            </button>
          ))}
        </div>
      </Menu>
    </>
  );
}
