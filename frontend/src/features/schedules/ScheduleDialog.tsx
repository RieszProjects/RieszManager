/** Manage one session's scheduled wake-ups (kept by the runner; they survive restarts). */
import {
  Button, Dialog, DialogActions, DialogContent, DialogTitle, IconButton, Switch, TextField, ToggleButton,
  ToggleButtonGroup, Tooltip,
} from "@mui/material";
import { AlarmClock, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { clockTime } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";
import { DAY_LABELS, useSchedules, type Schedule, type ScheduleInput } from "./store";

interface Draft {
  name: string;
  mode: "times" | "every";
  times: string;
  days: number[];
  every: string;
  jitter: string;
  catchUp: boolean;
  tz: string;
  prompt: string;
}

function draftFrom(s: Schedule | null, tz: string): Draft {
  return {
    name: s?.name ?? "",
    mode: s?.every ? "every" : "times",
    times: s?.times.join(", ") ?? "",
    days: s?.days ?? [],
    every: s?.every ? String(s.every) : "30",
    jitter: s?.jitter ? String(s.jitter) : "0",
    catchUp: s?.catchUp ?? false,
    tz: s?.tz ?? tz,
    prompt: s?.prompt ?? "",
  };
}

function inputFrom(d: Draft): ScheduleInput {
  const base: ScheduleInput = {
    name: d.name.trim(), prompt: d.prompt, jitter: Number(d.jitter) || 0, catchUp: d.catchUp, tz: d.tz.trim() || undefined,
  };
  if (d.mode === "every") return { ...base, every: Number(d.every) || 0, times: [], days: [] };
  return { ...base, times: d.times.split(/[,，\s]+/).map((t) => t.trim()).filter(Boolean), days: d.days, every: null };
}

function ScheduleForm({ initial, timezone, onCancel, onSave }: {
  initial: Schedule | null; timezone: string; onCancel: () => void; onSave: (input: ScheduleInput) => Promise<void>;
}) {
  const [d, setD] = useState<Draft>(() => draftFrom(initial, timezone));
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<Draft>) => setD((cur) => ({ ...cur, ...patch }));
  const save = async () => {
    setBusy(true);
    try {
      await onSave(inputFrom(d));
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rz-sched-form">
      <TextField size="small" label="名称" value={d.name} onChange={(e) => set({ name: e.target.value })} fullWidth />
      <ToggleButtonGroup size="small" exclusive value={d.mode} onChange={(_, v) => v && set({ mode: v })}>
        <ToggleButton value="times">固定时间</ToggleButton>
        <ToggleButton value="every">每隔一段时间</ToggleButton>
      </ToggleButtonGroup>
      {d.mode === "times" ? (
        <>
          <TextField size="small" label="时间" placeholder="13:00, 22:30" value={d.times} onChange={(e) => set({ times: e.target.value })} fullWidth />
          <div className="rz-sched-days">
            {DAY_LABELS.map((label, i) => (
              <button
                key={label}
                type="button"
                className={`rz-sched-day${d.days.includes(i) ? " on" : ""}`}
                onClick={() => set({ days: d.days.includes(i) ? d.days.filter((x) => x !== i) : [...d.days, i].sort() })}
              >
                {label}
              </button>
            ))}
            <span className="dim">{d.days.length ? "" : "每天"}</span>
          </div>
        </>
      ) : (
        <TextField size="small" label="间隔（分钟）" type="number" value={d.every} onChange={(e) => set({ every: e.target.value })} />
      )}
      <div className="rz-sched-row">
        <TextField size="small" label="随机推迟（分钟）" type="number" value={d.jitter} onChange={(e) => set({ jitter: e.target.value })} />
        <TextField size="small" label="时区" value={d.tz} onChange={(e) => set({ tz: e.target.value })} />
      </div>
      <label className="rz-sched-switch">
        <Switch size="small" checked={d.catchUp} onChange={(e) => set({ catchUp: e.target.checked })} />
        错过时补跑一次
      </label>
      <TextField
        label="发送内容"
        multiline
        minRows={3}
        value={d.prompt}
        onChange={(e) => set({ prompt: e.target.value })}
        helperText="{now} 会换成发送时的时间"
        fullWidth
      />
      <div className="rz-sched-actions">
        <Button onClick={onCancel} disabled={busy}>取消</Button>
        <Button variant="contained" onClick={() => void save()} disabled={busy || !d.prompt.trim()}>保存</Button>
      </div>
    </div>
  );
}

function ScheduleCard({ s, onEdit }: { s: Schedule; onEdit: () => void }) {
  const { update, remove, runNow } = useSchedules.getState();
  const [confirm, setConfirm] = useState(false);
  const act = (fn: () => Promise<void>) => void fn().catch((err: Error) => toast(err.message, "error"));
  return (
    <div className={`rz-sched-card${s.enabled ? "" : " off"}`}>
      <div className="rz-sched-card-main">
        <div className="rz-sched-card-title">{s.name || s.rule}</div>
        <div className="dim">{s.name ? `${s.rule} · ` : ""}{s.enabled && s.next ? `下次 ${clockTime(s.next)}` : "已暂停"}</div>
        {s.last && <div className="dim">上次 {clockTime(s.last)}{s.lastResult && s.lastResult !== "sent" ? ` · ${s.lastResult}` : ""}</div>}
        <div className="rz-sched-prompt clamp-1">{s.prompt}</div>
      </div>
      <Switch size="small" checked={s.enabled} onChange={(e) => act(() => update(s.id, { enabled: e.target.checked }))} />
      <Tooltip title="立即运行"><IconButton size="small" onClick={() => act(() => runNow(s.id))}><Play size={15} /></IconButton></Tooltip>
      <Tooltip title="编辑"><IconButton size="small" onClick={onEdit}><Pencil size={15} /></IconButton></Tooltip>
      <Tooltip title={confirm ? "再点一次删除" : "删除"}>
        <IconButton size="small" className={confirm ? "rz-danger" : ""}
          onClick={() => (confirm ? act(() => remove(s.id)) : (setConfirm(true), window.setTimeout(() => setConfirm(false), 3000)))}>
          <Trash2 size={15} />
        </IconButton>
      </Tooltip>
    </div>
  );
}

export function ScheduleDialog() {
  const sessionId = useUi((s) => s.scheduleSession);
  const setScheduleSession = useUi((s) => s.setScheduleSession);
  const session = useApp((s) => (sessionId ? s.sessions[sessionId] : undefined));
  const items = useSchedules((s) => s.items);
  const timezone = useSchedules((s) => s.timezone);
  const isMobile = useIsMobile();
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const mine = useMemo(() => items.filter((s) => s.sessionId === sessionId), [items, sessionId]);
  const close = () => {
    setEditing(null);
    setScheduleSession(null);
  };
  return (
    <Dialog open={Boolean(sessionId)} onClose={close} fullWidth maxWidth="sm" fullScreen={isMobile}>
      <DialogTitle sx={{ fontFamily: "var(--font-display)", fontSize: 21, display: "flex", alignItems: "center", gap: 1 }}>
        <AlarmClock size={18} /> 定时唤醒
        <span className="dim clamp-1" style={{ fontSize: 13, fontFamily: "var(--font-sans)" }}>{session ? sessionTitle(session) : ""}</span>
      </DialogTitle>
      <DialogContent>
        {editing ? (
          <ScheduleForm
            initial={editing === "new" ? null : editing}
            timezone={timezone}
            onCancel={() => setEditing(null)}
            onSave={async (input) => {
              if (editing === "new") await useSchedules.getState().create(sessionId as string, input);
              else await useSchedules.getState().update(editing.id, input);
              setEditing(null);
            }}
          />
        ) : (
          <div className="rz-sched-list">
            {mine.map((s) => <ScheduleCard key={s.id} s={s} onEdit={() => setEditing(s)} />)}
            {!mine.length && <div className="dim rz-sched-empty">还没有定时唤醒</div>}
          </div>
        )}
      </DialogContent>
      {!editing && (
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button startIcon={<Plus size={15} />} onClick={() => setEditing("new")}>添加定时</Button>
          <span className="rz-spacer" />
          <Button onClick={close}>关闭</Button>
        </DialogActions>
      )}
    </Dialog>
  );
}
