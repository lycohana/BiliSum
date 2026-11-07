import { useEffect, useRef, useState, type DragEvent } from "react";

import type { VideoCollection } from "../types";

const MAX_COLLECTION_COVER_BYTES = 10 * 1024 * 1024;
const ACCEPTED_COLLECTION_COVER_TYPES = new Set(["image/jpeg", "image/jpg", "image/png", "image/webp"]);

function validateCoverFile(file: File): string {
  const supportedExtension = /\.(?:jpe?g|png|webp)$/i.test(file.name);
  if ((file.type && !ACCEPTED_COLLECTION_COVER_TYPES.has(file.type)) || (!file.type && !supportedExtension)) {
    return "仅支持 JPEG、PNG 或 WebP 图片。";
  }
  if (file.size > MAX_COLLECTION_COVER_BYTES) {
    return "图片不能超过 10MB。";
  }
  if (file.size === 0) {
    return "图片内容为空，请重新选择。";
  }
  return "";
}

function readableCoverError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message.trim() : "";
  return message && !/^Internal Server Error$/i.test(message) ? message : fallback;
}

export function CollectionSettingsDialog({
  collection,
  onSaveCover,
  onResetCover,
  onClose,
}: {
  collection: VideoCollection;
  onSaveCover(file: File): Promise<void>;
  onResetCover(): Promise<void>;
  onClose(): void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [activeAction, setActiveAction] = useState<"save" | "reset" | null>(null);
  const busy = activeAction !== null;

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [busy, onClose]);

  function chooseFile(file: File | null) {
    if (!file) return;
    const validationError = validateCoverFile(file);
    if (validationError) {
      setSelectedFile(null);
      setPreviewUrl("");
      if (inputRef.current) inputRef.current.value = "";
      setErrorMessage(validationError);
      setStatusMessage("");
      return;
    }
    setSelectedFile(file);
    setPreviewUrl(URL.createObjectURL(file));
    setErrorMessage("");
    setStatusMessage("");
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (busy) return;
    chooseFile(event.dataTransfer.files[0] || null);
  }

  async function saveCover() {
    if (!selectedFile || busy) return;
    setActiveAction("save");
    setErrorMessage("");
    setStatusMessage("");
    try {
      await onSaveCover(selectedFile);
      setSelectedFile(null);
      setPreviewUrl("");
      if (inputRef.current) inputRef.current.value = "";
      setStatusMessage("合集封面已保存。外层合集卡片会同步使用新封面。");
    } catch (error) {
      setErrorMessage(readableCoverError(error, "合集封面保存失败，请稍后重试。"));
    } finally {
      setActiveAction(null);
    }
  }

  async function resetCover() {
    if (!collection.custom_cover_url || busy) return;
    setActiveAction("reset");
    setErrorMessage("");
    setStatusMessage("");
    try {
      await onResetCover();
      setSelectedFile(null);
      setPreviewUrl("");
      if (inputRef.current) inputRef.current.value = "";
      setStatusMessage("已恢复 B 站原封面。");
    } catch (error) {
      setErrorMessage(readableCoverError(error, "恢复原封面失败，请稍后重试。"));
    } finally {
      setActiveAction(null);
    }
  }

  const visibleCover = previewUrl || collection.cover_url || collection.source_cover_url || "";
  return (
    <div className="update-dialog-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div
        className="update-dialog collection-settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="collection-settings-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="update-dialog-header">
          <div className="collection-settings-heading">
            <h2 id="collection-settings-title">合集设置</h2>
            <span>{collection.title}</span>
          </div>
          <button className="close-button" type="button" aria-label="关闭合集设置" disabled={busy} onClick={onClose}>
            <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          </button>
        </header>
        <div className="update-dialog-body collection-settings-body">
          <section className="collection-cover-setting" aria-labelledby="collection-cover-setting-title">
            <div className="collection-cover-setting-copy">
              <strong id="collection-cover-setting-title">合集封面</strong>
              <span>支持 JPEG、PNG、WebP，最大 10MB。封面会以 16:9 居中裁切显示。</span>
            </div>
            <div className="collection-cover-setting-layout">
              <div className="collection-cover-preview">
                {visibleCover ? <img src={visibleCover} alt="合集封面预览" /> : <span>暂无封面</span>}
                <small>{previewUrl ? "待保存" : collection.custom_cover_url ? "自定义封面" : "B 站原封面"}</small>
              </div>
              <div
                className="collection-cover-dropzone"
                onDragOver={(event) => event.preventDefault()}
                onDrop={handleDrop}
              >
                <input
                  ref={inputRef}
                  className="collection-cover-file-input"
                  type="file"
                  accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
                  disabled={busy}
                  onChange={(event) => chooseFile(event.currentTarget.files?.[0] || null)}
                />
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 16.5V19a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2.5M8 9l4-4 4 4m-4-4v11" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>
                <strong>{selectedFile ? selectedFile.name : "选择一张新封面"}</strong>
                <span>{selectedFile ? `${(selectedFile.size / 1024 / 1024).toFixed(2)} MB · 保存前可继续更换` : "点击选择，或将图片拖到这里"}</span>
                <button className="secondary-button compact-button" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
                  {selectedFile ? "重新选择" : "选择图片"}
                </button>
              </div>
            </div>
          </section>
          {errorMessage ? <p className="collection-cover-message is-error" role="alert">{errorMessage}</p> : null}
          {statusMessage ? <p className="collection-cover-message is-success" role="status">{statusMessage}</p> : null}
        </div>
        <footer className="update-dialog-footer collection-settings-footer">
          <button className="secondary-button compact-button collection-cover-reset" type="button" disabled={busy || !collection.custom_cover_url} onClick={() => void resetCover()}>
            {activeAction === "reset" ? "正在恢复…" : "恢复原封面"}
          </button>
          <div>
            <button className="secondary-button compact-button" type="button" disabled={busy} onClick={onClose}>关闭</button>
            <button className="primary-button compact-button" type="button" disabled={busy || !selectedFile} onClick={() => void saveCover()}>
              {activeAction === "save" ? "正在保存…" : "保存封面"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
