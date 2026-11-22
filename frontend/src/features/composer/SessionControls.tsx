/** Harness, model and permission-mode pickers shown in the composer toolbar. */
import { ListItemText, ListSubheader, Menu, MenuItem } from "@mui/material";
import { Bot, Check, ChevronDown, Cpu, LoaderCircle, Shield } from "lucide-react";
import { useState } from "react";
import type { ApiRoute, HarnessInfo, HarnessMode, ModelInfo } from "@/api/types";
import { modelLabel, modeTier, PERMISSION_MODES, permissionLabel } from "@/lib/format";
import { harnessLabel } from "@/lib/harnesses";

interface ModeProps {
  value: string | null | undefined;
  allowBypass: boolean;
  onChange: (mode: string) => void;
  disabled?: boolean;
  /** Another harness's own modes, instead of Claude Code's. */
  options?: HarnessMode[] | null;
}

export function ModePicker({ value, allowBypass, onChange, disabled, options }: ModeProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const modes = options
    ? options.map((m) => ({ value: m.id, label: m.name, hint: m.description ?? "" }))
    : PERMISSION_MODES.filter((m) => allowBypass || m.value !== "bypassPermissions");
  const current = value ?? (options ? options[0]?.id ?? "" : "default");
  const tier = modeTier(current, options);
  const label = options ? modes.find((m) => m.value === current)?.label ?? current : permissionLabel(current);
  return (
    <>
      <button
        type="button"
        className={`rz-chip mode-${current} tier-${tier}`}
        disabled={disabled}
        onClick={(e) => setAnchor(e.currentTarget)}
        title="权限模式（Shift+Tab 切换）"
      >
        <Shield size={13} />
        <span>{label}</span>
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

export function cycleMode(current: string | null | undefined, allowBypass: boolean, options?: HarnessMode[] | null): string {
  const order = options?.length
    ? options.map((m) => m.id)
    : ["default", "acceptEdits", "plan", "auto", ...(allowBypass ? ["bypassPermissions"] : [])];
  const idx = order.indexOf(current ?? order[0]);
  return order[(idx + 1) % order.length];
}

interface HarnessProps {
  value: string;
  harnesses: HarnessInfo[];
  /** A new session: the harness it will run on. An existing one (`moving`): hand it over (may take a while). */
  onChange: (harness: string) => void | Promise<void>;
  moving?: boolean;
}

/** Which harness runs the session: picked for a new one, or the one an existing session moves to. */
export function HarnessPicker({ value, harnesses, onChange, moving }: HarnessProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const options = harnesses.some((h) => h.id === "claude")
    ? harnesses
    : [{ id: "claude", name: "Claude Code", available: true, reason: null as string | null }, ...harnesses];
  const pick = async (id: string) => {
    setAnchor(null);
    if (moving && id === value) return;
    setPending(id);
    try {
      await onChange(id);
    } finally {
      setPending(null);
    }
  };
  return (
    <>
      <button type="button" className={`rz-chip harness-${value}`} onClick={(e) => setAnchor(e.currentTarget)} disabled={Boolean(pending)}
        title={moving ? "转到其他工具继续这个会话" : "用哪个工具运行这个会话"}>
        {pending && moving ? <LoaderCircle size={13} className="spin" /> : <Bot size={13} />}
        <span>{pending && moving ? `正在转到 ${harnessLabel(pending)}` : harnessLabel(value)}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}>
        {options.map((h) => (
          <MenuItem key={h.id} selected={h.id === value} disabled={!h.available}
            onClick={() => void pick(h.id)}>
            <ListItemText primary={moving && h.id !== value ? `转到 ${h.name} 继续` : h.name}
              secondary={h.available ? undefined : h.reason} slotProps={{ secondary: { sx: { fontSize: 12 } } }} />
            {h.id === value && <Check size={15} />}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
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
  /** `route`: set when the model belongs to another model API than the current one. */
  onChange: (model: string, route?: string) => void;
  onEffort: (effort: string) => void;
  disabled?: boolean;
  /** The model APIs the harness can run on, each with its models; the menu then lists them by API. */
  routes?: ApiRoute[] | null;
  route?: string | null;
  /** Only the current API can be used (a Codex session stays on the API it started with). */
  routeLocked?: boolean;
}

export function ModelPicker({ value, effort, models, onChange, onEffort, disabled, routes, route, routeLocked }: ModelProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const grouped = Boolean(routes && routes.length > 1);
  const currentRoute = routes?.find((r) => r.id === route) ?? routes?.find((r) => r.available);
  const shown = currentRoute?.models?.length ? currentRoute.models : models;
  const current = resolveModel(shown, value);
  const levels = current?.supportedEffortLevels?.length ? current.supportedEffortLevels : EFFORT_LEVELS;
  const name = modelName(shown, value);
  const item = (m: ModelInfo, r?: ApiRoute) => {
    const here = !r || r.id === currentRoute?.id;
    const on = here && m.value === current?.value;
    return (
      <MenuItem key={`${r?.id ?? ""}|${m.value}`} selected={on} disabled={!here && routeLocked}
        onClick={() => { setAnchor(null); onChange(m.value, here ? undefined : r?.id); }}>
        <ListItemText primary={m.displayName} secondary={m.description} slotProps={{ secondary: { sx: { fontSize: 12 } } }} />
        {on && <Check size={15} />}
      </MenuItem>
    );
  };
  return (
    <>
      <button type="button" className="rz-chip" disabled={disabled || (!shown.length && !grouped)} onClick={(e) => setAnchor(e.currentTarget)} title="模型与推理强度">
        <Cpu size={13} />
        <span>{effort ? `${name} · ${effort}` : name}</span>
        <ChevronDown size={12} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "top", horizontal: "left" }} transformOrigin={{ vertical: "bottom", horizontal: "left" }}
        slotProps={{ paper: { sx: { maxHeight: "min(70vh, 560px)" } } }}>
        {grouped
          ? routes!.filter((r) => r.available).flatMap((r) => [
              <ListSubheader key={`h|${r.id}`} className="rz-route-head">
                {r.label}{r.plan ? ` · ${r.plan}` : ""}
                {routeLocked && r.id !== currentRoute?.id && <span className="dim">（这个会话不能换过去）</span>}
              </ListSubheader>,
              ...(r.models?.length
                ? r.models.map((m) => item(m, r))
                : [<MenuItem key={`e|${r.id}`} disabled><ListItemText primary={r.error ?? "加载中…"} /></MenuItem>]),
            ])
          : shown.map((m) => item(m))}
        {grouped && routes!.some((r) => !r.available) && (
          <div className="rz-route-off">
            {routes!.filter((r) => !r.available).map((r) => <div key={r.id}>{r.label}：{r.reason}</div>)}
          </div>
        )}
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
