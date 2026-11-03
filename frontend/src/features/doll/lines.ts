/** What the doll says. She is perpetually sleepy and short on words. */
import type { UsageWindow } from "./usage";

function pick<T>(list: readonly T[]): T {
  return list[Math.floor(Math.random() * list.length)];
}

const SQUISH = ["呜……", "别捏……", "脸要变形了。", "……再捏我就睡给你看。", "啾。", "唔，好吧，就一下。"] as const;
const SLEEPY = ["zzz……啊，你说。", "……刚才没睡着。", "嗯？……几点了。"] as const;
const BUSY = ["它们在干活，我在看着。", "有会话在跑，别催。", "……还在想，等等。"] as const;

export function squishLine(): string {
  return pick(SQUISH);
}

/** The line shown above the usage numbers when she is tapped. */
export function statusLine(opts: { peak: UsageWindow | null; limited: boolean; sleeping: boolean; busy: boolean }): string {
  const { peak, limited, sleeping, busy } = opts;
  if (limited) return "额度用完了。等重置之后再叫我。";
  if (!peak) return sleeping ? pick(SLEEPY) : busy ? pick(BUSY) : "……今天的用量我看不到。";
  const p = peak.percent;
  if (p >= 95) return "快没了……省着点用。";
  if (p >= 80) return "已经用掉一大半了。";
  if (sleeping) return pick(SLEEPY);
  if (busy) return pick(BUSY);
  if (p >= 50) return "过半了，还撑得住。";
  return pick(["还剩很多，慢慢来。", "额度还早。我再睡会儿。", "嗯，够用。"]);
}

export function waitingLine(count: number): string {
  return count > 1 ? `有 ${count} 个会话在等你点头。` : "有个会话在等你点头。";
}

export function warnLine(w: UsageWindow, name: string): string {
  return w.percent >= 95 ? `${name}用量到 ${Math.round(w.percent)}% 了……真的要没了。` : `${name}用量到 ${Math.round(w.percent)}% 了，留意一下。`;
}
