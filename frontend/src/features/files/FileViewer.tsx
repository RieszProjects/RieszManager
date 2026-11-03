/** Read-only file viewer (code, markdown, images, PDFs) shown as a dialog. */
import { Dialog, IconButton, Tooltip } from "@mui/material";
import { Code2, Copy, Download, Eye, LoaderCircle, WrapText, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, fileRawUrl } from "@/api/client";
import type { FileInfo } from "@/api/types";
import { CodeBlock } from "@/components/CodeBlock";
import { Markdown } from "@/components/Markdown";
import { copyText } from "@/lib/clipboard";
import { clockTime, fileSize } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";
import { toast, useUi } from "@/store/ui";

export function FileViewer() {
  const target = useUi((s) => s.file);
  const openFile = useUi((s) => s.openFile);
  const isMobile = useIsMobile();
  const [info, setInfo] = useState<FileInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState(true);
  const [wrap, setWrap] = useState(false);

  useEffect(() => {
    if (!target) return;
    setInfo(null);
    setError(null);
    setPreview(true);
    const ctrl = new AbortController();
    api<{ file: FileInfo }>("files/content", { query: { project: target.project, path: target.path }, signal: ctrl.signal })
      .then((res) => setInfo(res.file))
      .catch((err) => (err as Error).name !== "AbortError" && setError((err as Error).message));
    return () => ctrl.abort();
  }, [target]);

  const close = () => openFile(null);
  const isMarkdown = info?.language === "markdown";

  return (
    <Dialog open={Boolean(target)} onClose={close} fullScreen={isMobile} maxWidth={false}
      slotProps={{ paper: { className: "rz-fileviewer" } }}>
      {target && (
        <>
          <div className="rz-fileviewer-head">
            <div className="rz-fileviewer-title">
              <div className="mono clamp-1">{target.path}</div>
              {info && <div className="dim">{fileSize(info.size)} · 修改于 {clockTime(info.mtime)}{info.truncated ? " · 仅显示前面部分" : ""}</div>}
            </div>
            {info?.kind === "text" && isMarkdown && (
              <Tooltip title={preview ? "查看源码" : "预览"}>
                <IconButton size="small" onClick={() => setPreview(!preview)}>{preview ? <Code2 size={17} /> : <Eye size={17} />}</IconButton>
              </Tooltip>
            )}
            {info?.kind === "text" && (!isMarkdown || !preview) && (
              <Tooltip title="自动换行">
                <IconButton size="small" className={wrap ? "on" : ""} onClick={() => setWrap(!wrap)}><WrapText size={17} /></IconButton>
              </Tooltip>
            )}
            {info?.kind === "text" && (
              <Tooltip title="复制内容">
                <IconButton size="small" onClick={async () => (await copyText(info.content ?? "")) && toast("已复制", "success")}><Copy size={16} /></IconButton>
              </Tooltip>
            )}
            <Tooltip title="下载">
              <IconButton size="small" component="a" href={fileRawUrl(target.project, target.path, true)}><Download size={17} /></IconButton>
            </Tooltip>
            <IconButton size="small" onClick={close} aria-label="关闭"><X size={18} /></IconButton>
          </div>
          <div className="rz-fileviewer-body">
            {error && <div className="rz-center dim">{error}</div>}
            {!info && !error && <div className="rz-center"><LoaderCircle className="spin" size={20} /></div>}
            {info?.kind === "text" && (isMarkdown && preview ? (
              <div className="rz-fileviewer-md"><Markdown text={info.content ?? ""} /></div>
            ) : (
              <CodeBlock code={info.content ?? ""} language={info.language} lineNumbers wrap={wrap} />
            ))}
            {info?.kind === "image" && (
              <div className="rz-fileviewer-image"><img src={fileRawUrl(target.project, target.path)} alt={info.name} /></div>
            )}
            {info?.kind === "pdf" && <iframe className="rz-fileviewer-pdf" src={fileRawUrl(target.project, target.path)} title={info.name} />}
            {info?.kind === "binary" && (
              <div className="rz-center dim">二进制文件，无法预览。<a href={fileRawUrl(target.project, target.path, true)}>下载</a></div>
            )}
          </div>
        </>
      )}
    </Dialog>
  );
}
