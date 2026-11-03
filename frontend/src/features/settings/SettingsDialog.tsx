/** Settings: two-factor authentication, signed-in devices, login history, notifications. */
import { Button, Dialog, DialogContent, IconButton, Switch, Tab, Tabs, TextField } from "@mui/material";
import { Archive, Bell, Download, LogOut, MonitorSmartphone, ShieldCheck, ShieldOff, X } from "lucide-react";
import QRCode from "qrcode";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import { copyText } from "@/lib/clipboard";
import { clockTime } from "@/lib/format";
import { DollSettings } from "@/features/doll/DollSettings";
import { useIsMobile } from "@/lib/useIsMobile";
import { useApp } from "@/store/app";
import { useAuth } from "@/store/auth";
import { toast, useUi } from "@/store/ui";

interface Security {
  username: string;
  totpEnabled: boolean;
  totpRequired: boolean;
  recoveryCodesLeft: number;
  sessions: { id: number; current: boolean; createdAt: string; lastSeen: string; ip: string; userAgent: string }[];
  attempts: { at: string; ip: string; stage: string; success: boolean; userAgent: string }[];
}

function describeAgent(ua: string): string {
  const os = /iPhone|iPad/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Mac OS X/.test(ua) ? "macOS" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "未知系统";
  const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "浏览器";
  return `${browser} · ${os}`;
}

function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div className="rz-settings-block">
      <div className="rz-settings-title">恢复码</div>
      <p className="dim">每个恢复码只能使用一次。请妥善保存，它们只会显示这一次。</p>
      <div className="rz-recovery-grid mono">{codes.map((c) => <span key={c}>{c}</span>)}</div>
      <div className="rz-row-gap">
        <Button variant="outlined" onClick={async () => (await copyText(codes.join("\n"))) && toast("已复制", "success")}>复制</Button>
        <Button variant="contained" onClick={onDone}>我已保存</Button>
      </div>
    </div>
  );
}

function TotpSection({ data, reload }: { data: Security; reload: () => void }) {
  const [setup, setSetup] = useState<{ secret: string; uri: string; qr: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [disableCode, setDisableCode] = useState("");
  const [busy, setBusy] = useState(false);
  const refreshAuth = useAuth((s) => s.refresh);

  const begin = async () => {
    setBusy(true);
    try {
      const res = await api<{ secret: string; uri: string }>("account/totp/setup", { method: "POST" });
      const qr = await QRCode.toDataURL(res.uri, { margin: 1, width: 220, color: { dark: "#15181a", light: "#e8e2d4" } });
      setSetup({ ...res, qr });
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    try {
      const res = await api<{ recoveryCodes: string[] }>("account/totp/confirm", { body: { code } });
      setCodes(res.recoveryCodes);
      setSetup(null);
      setCode("");
      reload();
      void refreshAuth();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    try {
      await api("account/totp/disable", { body: { code: disableCode, recovery: disableCode.includes("-") } });
      setDisableCode("");
      toast("已关闭两步验证", "success");
      reload();
      void refreshAuth();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    setBusy(true);
    try {
      const res = await api<{ recoveryCodes: string[] }>("account/recovery/regenerate", { body: { code: disableCode } });
      setCodes(res.recoveryCodes);
      setDisableCode("");
      reload();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  if (codes) return <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />;

  if (setup) {
    return (
      <div className="rz-settings-block">
        <div className="rz-settings-title">设置验证器</div>
        <p className="dim">用 Google Authenticator、1Password、Aegis 等应用扫描二维码，然后输入生成的 6 位验证码。</p>
        <div className="rz-totp-setup">
          <img src={setup.qr} alt="TOTP 二维码" />
          <div>
            <div className="dim">无法扫码时手动输入密钥：</div>
            <button type="button" className="rz-secret mono" onClick={async () => (await copyText(setup.secret)) && toast("已复制密钥", "success")}>
              {setup.secret.replace(/(.{4})/g, "$1 ").trim()}
            </button>
            <TextField fullWidth placeholder="6 位验证码" value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              slotProps={{ htmlInput: { inputMode: "numeric", className: "rz-otp-input small" } }} sx={{ mt: 1.5 }} />
            <div className="rz-row-gap">
              <Button variant="contained" disabled={code.length !== 6 || busy} onClick={confirm}>启用</Button>
              <Button variant="outlined" onClick={() => setSetup(null)}>取消</Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rz-settings-block">
      <div className="rz-settings-title">
        {data.totpEnabled ? <ShieldCheck size={17} className="ok" /> : <ShieldOff size={17} className="warn" />}
        两步验证 {data.totpEnabled ? "已启用" : "未启用"}
      </div>
      {data.totpEnabled ? (
        <>
          <p className="dim">剩余恢复码 {data.recoveryCodesLeft} 个。关闭两步验证或重新生成恢复码需要输入当前验证码（或一个恢复码）。</p>
          <TextField fullWidth placeholder="验证码或恢复码" value={disableCode} onChange={(e) => setDisableCode(e.target.value.slice(0, 16))} />
          <div className="rz-row-gap">
            <Button variant="outlined" disabled={!disableCode || busy} onClick={regenerate}>重新生成恢复码</Button>
            {!data.totpRequired && <Button variant="outlined" color="error" disabled={!disableCode || busy} onClick={disable}>关闭两步验证</Button>}
          </div>
        </>
      ) : (
        <>
          <p className="dim">登录时除密码外还需要验证器中的动态验证码。强烈建议启用。</p>
          <Button variant="contained" disabled={busy} onClick={begin}>开始设置</Button>
        </>
      )}
    </div>
  );
}

function DevicesSection({ data, reload }: { data: Security; reload: () => void }) {
  const revoke = async (id?: number) => {
    try {
      const res = await api<{ revoked: number }>("account/sessions/revoke", { body: id === undefined ? {} : { id } });
      toast(`已退出 ${res.revoked} 个设备`, "success");
      reload();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };
  return (
    <div className="rz-settings-block">
      <div className="rz-settings-title"><MonitorSmartphone size={17} /> 已登录的设备</div>
      <div className="rz-device-list">
        {data.sessions.map((s) => (
          <div key={s.id} className="rz-device">
            <div>
              <div>{describeAgent(s.userAgent)} {s.current && <span className="rz-tag">当前</span>}</div>
              <div className="dim mono">{s.ip} · 最近活动 {clockTime(s.lastSeen)}</div>
            </div>
            {!s.current && <Button size="small" variant="outlined" color="error" onClick={() => void revoke(s.id)}>退出</Button>}
          </div>
        ))}
      </div>
      {data.sessions.length > 1 && <Button variant="outlined" color="error" onClick={() => void revoke()}>退出其他所有设备</Button>}
      <div className="rz-settings-title" style={{ marginTop: 22 }}>最近的登录尝试</div>
      <div className="rz-attempts">
        {data.attempts.map((a, i) => (
          <div key={i} className={`rz-attempt${a.success ? "" : " fail"}`}>
            <span>{clockTime(a.at)}</span>
            <span className="mono">{a.ip}</span>
            <span>{a.stage === "totp" ? "验证码" : "密码"}{a.success ? "通过" : "失败"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function GeneralSection() {
  const config = useApp((s) => s.config);
  const showArchived = useUi((s) => s.showArchived);
  const setShowArchived = useUi((s) => s.setShowArchived);
  const logout = useAuth((s) => s.logout);
  const [perm, setPerm] = useState(typeof Notification !== "undefined" ? Notification.permission : "denied");
  const [installEvent, setInstallEvent] = useState<(Event & { prompt: () => Promise<void> }) | null>(
    (window as unknown as { __rzInstall?: Event & { prompt: () => Promise<void> } }).__rzInstall ?? null,
  );
  useEffect(() => {
    const handler = (e: Event) => {
      e.preventDefault();
      setInstallEvent(e as Event & { prompt: () => Promise<void> });
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);
  return (
    <div className="rz-settings-block">
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title"><Bell size={17} /> 桌面通知</div>
          <div className="dim">页面在后台时，Claude 等待确认会弹出通知</div>
        </div>
        <Switch checked={perm === "granted"} disabled={perm === "denied" || typeof Notification === "undefined"}
          onChange={async () => setPerm(await Notification.requestPermission())} />
      </div>
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title"><Archive size={17} /> 显示已归档的会话和项目</div>
          <div className="dim">只影响这台设备上的列表</div>
        </div>
        <Switch checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
      </div>
      <DollSettings />
      {installEvent && (
        <div className="rz-settings-row">
          <div>
            <div className="rz-settings-title"><Download size={17} /> 安装应用</div>
            <div className="dim">安装到桌面或主屏幕，像原生应用一样使用</div>
          </div>
          <Button variant="outlined" onClick={() => void installEvent.prompt().then(() => setInstallEvent(null))}>安装</Button>
        </div>
      )}
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title">工作区</div>
          <div className="dim mono">{config?.workspaceRoot ?? "—"}</div>
        </div>
      </div>
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title">账号</div>
          <div className="dim">{config?.username}</div>
        </div>
        <Button variant="outlined" color="error" startIcon={<LogOut size={15} />} onClick={() => void logout()}>退出登录</Button>
      </div>
    </div>
  );
}

export function SettingsDialog() {
  const open = useUi((s) => s.settingsOpen);
  const setOpen = useUi((s) => s.setSettingsOpen);
  const forced = useAuth((s) => s.totpSetupRequired);
  const isMobile = useIsMobile();
  const [tab, setTab] = useState(0);
  const [data, setData] = useState<Security | null>(null);

  const reload = useCallback(() => {
    api<Security>("account/security").then(setData).catch((err) => toast((err as Error).message, "error"));
  }, []);

  useEffect(() => {
    if (open || forced) reload();
  }, [open, forced, reload]);

  const visible = open || forced;
  const activeTab = forced ? 1 : tab;
  return (
    <Dialog open={visible} onClose={() => !forced && setOpen(false)} fullScreen={isMobile} fullWidth maxWidth="sm">
      <div className="rz-settings-head">
        <span className="display">{forced ? "请先启用两步验证" : "设置"}</span>
        {!forced && <IconButton size="small" onClick={() => setOpen(false)} aria-label="关闭"><X size={18} /></IconButton>}
      </div>
      {!forced && (
        <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ px: 2, borderBottom: "1px solid var(--line)" }}>
          <Tab label="通用" />
          <Tab label="安全" />
          <Tab label="设备" />
        </Tabs>
      )}
      <DialogContent sx={{ pt: 2.5 }}>
        {activeTab === 0 && <GeneralSection />}
        {activeTab === 1 && data && <TotpSection data={data} reload={reload} />}
        {activeTab === 2 && data && <DevicesSection data={data} reload={reload} />}
      </DialogContent>
    </Dialog>
  );
}
