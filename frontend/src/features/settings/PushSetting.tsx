/** Settings row: push notifications for this browser. */
import { Button, Switch } from "@mui/material";
import { Bell } from "lucide-react";
import { useEffect, useState } from "react";
import { disablePush, enablePush, pushState, testPush, type PushState } from "@/lib/push";
import { toast } from "@/store/ui";

const HINT: Record<PushState, string> = {
  on: "Claude 等你确认、向你提问、完成运行超过 1 分钟的任务或意外中断时提醒你；你正在看的会话不提醒",
  off: "Claude 等你确认、向你提问、完成运行超过 1 分钟的任务或意外中断时提醒你；你正在看的会话不提醒",
  denied: "通知权限被拒绝了：请在浏览器的网站设置里允许通知，然后再打开",
  unsupported: "这个浏览器不支持推送（iPhone 需要先“添加到主屏幕”，再从主屏幕打开）",
};

export function PushSetting() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    pushState().then(setState, () => setState("unsupported"));
  }, []);

  const toggle = async (on: boolean) => {
    setBusy(true);
    try {
      const next = on ? await enablePush() : await disablePush();
      setState(next);
      if (on && next === "on") toast("推送通知已开启", "success");
    } catch (err) {
      toast(`开启推送失败：${(err as Error).message}`, "error");
      setState(await pushState().catch(() => "off" as const));
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    try {
      await testPush();
      toast("测试通知已发出", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rz-settings-row">
      <div>
        <div className="rz-settings-title"><Bell size={17} /> 推送通知</div>
        <div className="dim">{state ? HINT[state] : "…"}</div>
        {state === "on" && (
          <Button size="small" variant="outlined" sx={{ mt: 1 }} disabled={busy} onClick={() => void test()}>
            发送测试通知
          </Button>
        )}
      </div>
      <Switch
        checked={state === "on"}
        disabled={busy || state === null || state === "unsupported" || state === "denied"}
        onChange={(e) => void toggle(e.target.checked)}
      />
    </div>
  );
}
