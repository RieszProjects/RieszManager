/** The app's mascot artwork (sleepy cat-eared girl). Falls back gracefully if an image is missing. */
import { useState, type CSSProperties } from "react";

export type MascotPose = "icon" | "full" | "sleepy" | "working" | "sleeping" | "confused";

const SOURCES: Record<MascotPose, string> = {
  icon: "/mascot/icon.webp",
  full: "/mascot/full.webp",
  sleepy: "/mascot/sleepy.webp",
  working: "/mascot/chibi_working.webp",
  sleeping: "/mascot/chibi_sleeping.webp",
  confused: "/mascot/chibi_confused.webp",
};

interface Props {
  pose: MascotPose;
  size?: number | string;
  height?: number | string;
  className?: string;
  style?: CSSProperties;
  alt?: string;
}

export function Mascot({ pose, size, height, className, style, alt = "" }: Props) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      src={SOURCES[pose]}
      alt={alt}
      draggable={false}
      className={className}
      onError={() => setFailed(true)}
      style={{ width: size, height: height ?? (size && pose === "icon" ? size : undefined), objectFit: "contain", userSelect: "none", ...style }}
    />
  );
}
