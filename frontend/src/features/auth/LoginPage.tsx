import { Button, Checkbox, FormControlLabel, IconButton, InputAdornment, TextField } from "@mui/material";
import { Eye, EyeOff, KeyRound, LoaderCircle } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Mascot } from "@/components/Mascot";
import { useAuth } from "@/store/auth";

function PasswordStep() {
  const login = useAuth((s) => s.login);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password) return;
    setBusy(true);
    setError(null);
    try {
      await login(username, password, remember);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="rz-login-form" onSubmit={submit}>
      <TextField label="用户名" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username"
        autoFocus fullWidth size="medium" />
      <TextField
        label="密码"
        type={show ? "text" : "password"}
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoComplete="current-password"
        fullWidth
        size="medium"
        slotProps={{
          input: {
            endAdornment: (
              <InputAdornment position="end">
                <IconButton size="small" onClick={() => setShow(!show)} aria-label={show ? "隐藏密码" : "显示密码"}>
                  {show ? <EyeOff size={17} /> : <Eye size={17} />}
                </IconButton>
              </InputAdornment>
            ),
          },
        }}
      />
      <FormControlLabel control={<Checkbox checked={remember} onChange={(e) => setRemember(e.target.checked)} size="small" />}
        label="在此设备上保持登录" sx={{ "& .MuiFormControlLabel-label": { fontSize: 13.5, color: "var(--text2)" } }} />
      {error && <div className="rz-login-error">{error}</div>}
      <Button type="submit" variant="contained" size="large" disabled={busy || !username || !password}>
        {busy ? <LoaderCircle size={18} className="spin" /> : "登录"}
      </Button>
    </form>
  );
}

function TotpStep() {
  const verify = useAuth((s) => s.verify);
  const refresh = useAuth((s) => s.refresh);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (value = code) => {
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      await verify(value, recovery);
    } catch (err) {
      setError((err as Error).message);
      setCode("");
      if ((err as { code?: string }).code === "pending_expired") await refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="rz-login-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <div className="rz-login-step"><KeyRound size={16} /> {recovery ? "输入一个恢复码" : "输入验证器中的 6 位验证码"}</div>
      <TextField
        value={code}
        onChange={(e) => {
          const v = recovery ? e.target.value.slice(0, 16) : e.target.value.replace(/\D/g, "").slice(0, 6);
          setCode(v);
          if (!recovery && v.length === 6) void submit(v);
        }}
        autoFocus
        fullWidth
        size="medium"
        placeholder={recovery ? "xxxxx-xxxxx" : "000000"}
        slotProps={{
          htmlInput: {
            inputMode: recovery ? "text" : "numeric",
            autoComplete: "one-time-code",
            className: `rz-otp-input${recovery ? " recovery" : ""}`,
          },
        }}
      />
      {error && <div className="rz-login-error">{error}</div>}
      <Button type="submit" variant="contained" size="large" disabled={busy || !code}>
        {busy ? <LoaderCircle size={18} className="spin" /> : "验证"}
      </Button>
      <div className="rz-login-links">
        <button type="button" className="rz-linklike" onClick={() => { setRecovery(!recovery); setCode(""); setError(null); }}>
          {recovery ? "使用验证码" : "使用恢复码"}
        </button>
        <button type="button" className="rz-linklike" onClick={() => void refresh().then(() => window.location.reload())}>
          返回
        </button>
      </div>
    </form>
  );
}

export function LoginPage() {
  const status = useAuth((s) => s.status);
  return (
    <div className="rz-login">
      <div className="rz-login-art">
        <Mascot pose="full" className="rz-login-mascot" />
        <div className="rz-login-art-caption">
          <span className="display">Riesz Manager</span>
        </div>
      </div>
      <div className="rz-login-panel">
        <div className="rz-login-card">
          <Mascot pose="icon" size={64} className="rz-login-icon" />
          <h1 className="rz-login-title">Riesz Manager</h1>
          <p className="rz-login-sub">{status === "totp" ? "两步验证" : "登录以继续"}</p>
          {status === "totp" ? <TotpStep /> : <PasswordStep />}
        </div>
      </div>
    </div>
  );
}
