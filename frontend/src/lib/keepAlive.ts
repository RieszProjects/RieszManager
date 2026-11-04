/** Turn "keep running" on or off for a session (see the runner's keep-alive endpoint). */
import { api } from "@/api/client";
import { toast } from "@/store/ui";

export async function setKeepAlive(sessionId: string, on: boolean): Promise<void> {
  const res = await api<{ note: string | null }>(`sessions/${sessionId}/keep-alive`, { body: { on } });
  if (!on) toast("已关闭保持运行，空闲一段时间后会照常结束", "success");
  else if (res.note) toast(`已开启保持运行，但暂时没能启动：${res.note}`, "warning");
  else toast("已开启保持运行：空闲时不会被关闭，重启后会自动恢复", "success");
}
