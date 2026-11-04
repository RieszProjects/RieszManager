/** Settings rows for the doll widget, shown in Settings → General. */
import { Button, IconButton, MenuItem, Select, Switch, ToggleButton, ToggleButtonGroup, Tooltip } from "@mui/material";
import { MessageSquareText, Play, Volume2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Mascot } from "@/components/Mascot";
import { usePhrases } from "./phrases";
import { PhrasesDialog } from "./PhrasesDialog";
import { useDollPrefs, type DollSize } from "./prefs";
import { squeak, VOICES, type Voice } from "./sound";

export const SIZE_LABELS: Record<DollSize, string> = { s: "小", m: "中", l: "大" };

export function DollSettings() {
  const { enabled, sound, voice, size, update } = useDollPrefs();
  const phrases = usePhrases((s) => s.phrases);
  const loadPhrases = usePhrases((s) => s.load);
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (enabled) loadPhrases().catch(() => undefined);
  }, [enabled, loadPhrases]);
  return (
    <>
      <div className="rz-settings-row">
        <div>
          <div className="rz-settings-title"><Mascot pose="icon" size={18} /> 莉思玩偶</div>
          <div className="dim">让看板娘莉思从角落探出头来，可以捏，点她查看套餐用量。只影响这台设备</div>
        </div>
        <Switch checked={enabled} onChange={(e) => update({ enabled: e.target.checked })} />
      </div>
      {enabled && (
        <>
          <div className="rz-settings-row rz-settings-sub">
            <div className="rz-settings-title"><Volume2 size={16} /> 捏的时候发出声音</div>
            <div className="rz-row-controls">
              {sound && (
                <>
                  <Select size="small" value={voice} onChange={(e) => {
                    const next = e.target.value as Voice;
                    update({ voice: next });
                    squeak.demo(next);
                  }} sx={{ minWidth: 112, fontSize: 13.5 }}>
                    {(Object.keys(VOICES) as Voice[]).map((v) => <MenuItem key={v} value={v}>{VOICES[v]}</MenuItem>)}
                  </Select>
                  <Tooltip title="试听">
                    <IconButton size="small" onClick={() => squeak.demo(voice)} aria-label="试听"><Play size={15} /></IconButton>
                  </Tooltip>
                </>
              )}
              <Switch checked={sound} onChange={(e) => update({ sound: e.target.checked })} />
            </div>
          </div>
          <div className="rz-settings-row rz-settings-sub">
            <div className="rz-settings-title">大小</div>
            <ToggleButtonGroup size="small" exclusive value={size} onChange={(_, v: DollSize | null) => v && update({ size: v })}>
              {(Object.keys(SIZE_LABELS) as DollSize[]).map((k) => (
                <ToggleButton key={k} value={k} sx={{ px: 1.6, py: 0.3 }}>{SIZE_LABELS[k]}</ToggleButton>
              ))}
            </ToggleButtonGroup>
          </div>
          <div className="rz-settings-row rz-settings-sub">
            <div className="rz-settings-title">
              <MessageSquareText size={16} /> 快捷短语
              <span className="dim">{phrases.length ? `${phrases.length} 条` : "未设置"}</span>
            </div>
            <Button size="small" variant="outlined" onClick={() => setEditing(true)}>编辑</Button>
          </div>
          <PhrasesDialog open={editing} onClose={() => setEditing(false)} />
        </>
      )}
    </>
  );
}
