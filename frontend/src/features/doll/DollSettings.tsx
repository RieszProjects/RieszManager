/** Settings rows for the doll widget, shown in Settings → General. */
import { Switch, ToggleButton, ToggleButtonGroup } from "@mui/material";
import { Volume2 } from "lucide-react";
import { Mascot } from "@/components/Mascot";
import { useDollPrefs, type DollSize } from "./prefs";

export const SIZE_LABELS: Record<DollSize, string> = { s: "小", m: "中", l: "大" };

export function DollSettings() {
  const { enabled, sound, size, update } = useDollPrefs();
  return (
    <>
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title"><Mascot pose="icon" size={18} /> 看板娘玩偶</div>
          <div className="dim">在角落放一只可以捏的看板娘，点她查看套餐用量。只影响这台设备</div>
        </div>
        <Switch checked={enabled} onChange={(e) => update({ enabled: e.target.checked })} />
      </div>
      {enabled && (
        <>
          <div className="rz-settings-row rz-settings-sub">
            <div className="rz-settings-title"><Volume2 size={16} /> 捏的时候发出声音</div>
            <Switch checked={sound} onChange={(e) => update({ sound: e.target.checked })} />
          </div>
          <div className="rz-settings-row rz-settings-sub">
            <div className="rz-settings-title">大小</div>
            <ToggleButtonGroup size="small" exclusive value={size} onChange={(_, v: DollSize | null) => v && update({ size: v })}>
              {(Object.keys(SIZE_LABELS) as DollSize[]).map((k) => (
                <ToggleButton key={k} value={k} sx={{ px: 1.6, py: 0.3 }}>{SIZE_LABELS[k]}</ToggleButton>
              ))}
            </ToggleButtonGroup>
          </div>
        </>
      )}
    </>
  );
}
