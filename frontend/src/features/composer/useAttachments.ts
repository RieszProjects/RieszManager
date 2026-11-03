/** Upload management for files attached to the composer. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { UploadMeta } from "@/api/types";
import { toast } from "@/store/ui";

export interface Attachment {
  localId: number;
  name: string;
  size: number;
  mime: string;
  status: "uploading" | "done" | "error";
  upload?: UploadMeta;
  previewUrl?: string;
}

const MAX_IMAGE_EDGE = 2000;
let nextId = 0;

/** Downscale very large images (keeps uploads small and within API limits). */
async function prepareImage(file: File): Promise<File> {
  if (!file.type.startsWith("image/") || file.type === "image/gif" || file.type === "image/svg+xml") return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
    if (scale >= 1 && file.size < 3.5 * 1024 * 1024) return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

export function useAttachments() {
  const [items, setItems] = useState<Attachment[]>([]);
  const urls = useRef(new Set<string>());

  useEffect(() => () => urls.current.forEach((u) => URL.revokeObjectURL(u)), []);

  const add = useCallback(async (files: File[]) => {
    for (const original of files) {
      const localId = ++nextId;
      const file = await prepareImage(original);
      const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
      if (previewUrl) urls.current.add(previewUrl);
      setItems((cur) => [...cur, { localId, name: file.name || "粘贴的图片.png", size: file.size, mime: file.type, status: "uploading", previewUrl }]);
      const form = new FormData();
      form.append("file", file, file.name || "pasted.png");
      try {
        const res = await api<{ upload: UploadMeta }>("uploads", { body: form });
        setItems((cur) => cur.map((a) => (a.localId === localId ? { ...a, status: "done", upload: res.upload } : a)));
      } catch (err) {
        toast(`上传失败：${(err as Error).message}`, "error");
        setItems((cur) => cur.map((a) => (a.localId === localId ? { ...a, status: "error" } : a)));
      }
    }
  }, []);

  const remove = useCallback((localId: number) => setItems((cur) => cur.filter((a) => a.localId !== localId)), []);
  const clear = useCallback(() => setItems([]), []);

  return {
    items,
    add,
    remove,
    clear,
    uploading: items.some((a) => a.status === "uploading"),
    ids: items.filter((a) => a.status === "done" && a.upload).map((a) => a.upload!.id),
  };
}
