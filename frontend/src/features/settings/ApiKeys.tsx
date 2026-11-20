/** Settings → model APIs: the harnesses' subscription logins, and one API key per provider for every harness. */
import { Button, TextField, ToggleButton, ToggleButtonGroup } from "@mui/material";
import { CircleCheck, CircleDashed, KeyRound } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ProviderKey, SubscriptionLogin } from "@/api/types";
import { invalidateAllCommands } from "@/features/composer/useCommands";
import { refreshHarnesses } from "@/lib/harnesses";
import { toast } from "@/store/ui";

interface Listing {
  providers: ProviderKey[];
  subscriptions: SubscriptionLogin[];
}

interface Result {
  ok: boolean;
  detail?: string;
  providers: ProviderKey[];
}

const LOGIN_COMMAND: Record<string, string> = { anthropic: "claude auth login", openai: "codex login", gemini: "agy acp --login" };

function KeyRow({ p, onChanged }: { p: ProviderKey; onChanged: (list: ProviderKey[]) => void }) {
  const [editing, setEditing] = useState(false);
  const [kind, setKind] = useState<"key" | "command">(p.source === "command" ? "command" : "key");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (action: "set" | "test" | "clear", body?: Record<string, string>) => {
    setBusy(true);
    try {
      const res = await api<Result>(`credentials/${p.id}/${action}`, { body: body ?? {} });
      onChanged(res.providers);
      if (action !== "clear") toast(res.ok ? `${p.name}：${res.detail ?? "可用"}` : `${p.name}：${res.detail ?? "不可用"}`, res.ok ? "success" : "error");
      if (action !== "test") {
        refreshHarnesses();
        invalidateAllCommands();
        setEditing(false);
        setValue("");
      }
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rz-key-row">
      <div className="rz-settings-row">
        <div className="rz-key-main">
          <div className="rz-settings-title">
            {p.configured ? <CircleCheck size={16} className="ok" /> : <CircleDashed size={16} />} {p.name}
          </div>
          <div className="dim mono clamp-1">
            {p.source === "command" ? `命令：${p.command}` : p.source === "key" ? "已保存 key" : "未设置"}
          </div>
        </div>
        <div className="rz-row-gap">
          {p.configured && !editing && <Button size="small" variant="outlined" disabled={busy} onClick={() => void run("test")}>测试</Button>}
          {!editing && (
            <Button size="small" variant="outlined" disabled={busy} onClick={() => { setKind(p.source === "command" ? "command" : "key"); setValue(p.source === "command" ? p.command ?? "" : ""); setEditing(true); }}>
              {p.configured ? "修改" : "设置"}
            </Button>
          )}
          {p.configured && !editing && (
            <Button size="small" variant="outlined" color="error" disabled={busy} onClick={() => void run("clear")}>清除</Button>
          )}
        </div>
      </div>
      {editing && (
        <div className="rz-key-form">
          <ToggleButtonGroup size="small" exclusive value={kind} onChange={(_, v) => { if (v) { setKind(v); setValue(""); } }}>
            <ToggleButton value="key">粘贴 key</ToggleButton>
            <ToggleButton value="command">用命令获取</ToggleButton>
          </ToggleButtonGroup>
          <TextField
            autoFocus
            fullWidth
            size="small"
            type={kind === "key" ? "password" : "text"}
            autoComplete="off"
            spellCheck={false}
            placeholder={kind === "key" ? `${p.name} API key` : "输出 key 的命令"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && value.trim()) void run("set", { [kind]: value }); }}
            slotProps={{ htmlInput: { className: "mono" } }}
          />
          <div className="rz-row-gap">
            <Button size="small" variant="contained" disabled={busy || !value.trim()} onClick={() => void run("set", { [kind]: value })}>保存</Button>
            <Button size="small" disabled={busy} onClick={() => { setEditing(false); setValue(""); }}>取消</Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ApiKeysSection() {
  const [data, setData] = useState<Listing | null>(null);
  const load = useCallback(() => {
    api<Listing>("credentials").then(setData).catch((err) => toast((err as Error).message, "error"));
  }, []);
  useEffect(load, [load]);
  if (!data) return null;
  return (
    <div className="rz-settings-block">
      <div className="rz-settings-title">订阅</div>
      {data.subscriptions.map((s) => (
        <div key={s.provider} className="rz-settings-row">
          <div>
            <div className="rz-settings-title">
              {s.loggedIn ? <CircleCheck size={16} className="ok" /> : <CircleDashed size={16} />} {s.label}
            </div>
            <div className="dim">{s.loggedIn ? `已登录${s.plan ? ` · ${s.plan}` : ""}` : <>未登录：在服务器上运行 <span className="mono">{LOGIN_COMMAND[s.provider]}</span></>}</div>
          </div>
        </div>
      ))}
      <div className="rz-settings-title" style={{ marginTop: 10 }}><KeyRound size={17} /> API key</div>
      {data.providers.map((p) => (
        <KeyRow key={p.id} p={p} onChanged={(providers) => setData((d) => (d ? { ...d, providers } : d))} />
      ))}
    </div>
  );
}
