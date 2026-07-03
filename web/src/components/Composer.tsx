import { ClipboardEvent, DragEvent, FormEvent, KeyboardEvent, memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { apiUrl, uploadAttachment } from "../api/finclaw";
import type { AttachmentMeta } from "../types";

type Props = {
  sessionId: string;
  active: boolean;
  submitting: boolean;
  inputDisabled: boolean;
  submitDisabled: boolean;
  stopDisabled: boolean;
  referencedAttachments: AttachmentMeta[];
  onRemoveReferencedAttachment: (attachmentId: string) => void;
  onClearReferencedAttachments: () => void;
  onSubmit: (text: string, attachments: AttachmentMeta[], referencedAttachmentIds: string[]) => void;
  onStop: () => void;
  onStartResearch: (text: string, attachments: AttachmentMeta[], referencedAttachmentIds: string[]) => void;
  placeholder?: string;
};

let textMeasureCanvas: HTMLCanvasElement | undefined;

export const Composer = memo(function Composer({
  sessionId,
  active,
  submitting,
  inputDisabled,
  submitDisabled,
  stopDisabled,
  referencedAttachments,
  onRemoveReferencedAttachment,
  onClearReferencedAttachments,
  onSubmit,
  onStop,
  onStartResearch,
  placeholder = "Ask FinClaw...",
}: Props) {
  const [input, setInput] = useState("");
  const [researchArmed, setResearchArmed] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [attachments, setAttachments] = useState<AttachmentMeta[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [privacyDismissed, setPrivacyDismissed] = useState(() => window.localStorage.getItem("finclaw.image_privacy_dismissed") === "1");
  const formRef = useRef<HTMLFormElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const modeRef = useRef<HTMLSpanElement | null>(null);
  const actionsRef = useRef<HTMLDivElement | null>(null);
  const text = input.trim();
  const preparingSubmit = submitting && !active;
  const sendLocked = active || preparingSubmit || submitDisabled;
  const hasImageContext = attachments.length > 0 || referencedAttachments.length > 0;

  useLayoutEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    const computed = window.getComputedStyle(node);
    const longestLineWidth = measureLongestLine(input, computed);
    const compactInputWidth = getCompactInputWidth(formRef.current, menuRef.current, modeRef.current, actionsRef.current, node);
    const exceedsCompactLine = Boolean(input.trim()) && longestLineWidth > compactInputWidth;
    const nextExpanded = input.includes("\n") || exceedsCompactLine || hasImageContext || Boolean(uploadError);
    setExpanded(nextExpanded);
    node.style.height = `${Math.min(node.scrollHeight, nextExpanded ? 210 : 38)}px`;
  }, [input, expanded, hasImageContext, uploadError]);

  useEffect(() => {
    if (!menuOpen) return;
    function handlePointerDown(event: PointerEvent) {
      const node = menuRef.current;
      if (!node || !(event.target instanceof Node) || node.contains(event.target)) return;
      setMenuOpen(false);
    }
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [menuOpen]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (sendLocked || uploading) return;
    if (!text && !hasImageContext) return;
    const shouldStartResearch = researchArmed && !active;
    const currentAttachments = attachments;
    const referencedIds = referencedAttachments.map((item) => item.attachment_id);
    setInput("");
    setAttachments([]);
    setExpanded(false);
    setResearchArmed(false);
    setMenuOpen(false);
    onClearReferencedAttachments();
    if (shouldStartResearch) {
      onStartResearch(text, currentAttachments, referencedIds);
      return;
    }
    onSubmit(text, currentAttachments, referencedIds);
  }

  function startResearch() {
    if (active || sendLocked) return;
    setResearchArmed((value) => !value);
    setMenuOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      researchArmed &&
      !input &&
      (event.key === "Backspace" || event.key === "Delete") &&
      event.currentTarget.selectionStart === 0 &&
      event.currentTarget.selectionEnd === 0
    ) {
      event.preventDefault();
      setResearchArmed(false);
      return;
    }
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  async function handleFiles(files: FileList | File[]) {
    const incoming = Array.from(files).filter((file) => file.type.startsWith("image/"));
    if (!incoming.length) return;
    const remaining = Math.max(0, 4 - attachments.length);
    if (!remaining) {
      setUploadError("单轮最多上传 4 张图片");
      return;
    }
    setUploadError("");
    setUploading(true);
    try {
      const uploaded: AttachmentMeta[] = [];
      for (const file of incoming.slice(0, remaining)) {
        uploaded.push(await uploadAttachment(sessionId, file));
      }
      setAttachments((prev) => [...prev, ...uploaded].slice(0, 4));
      if (!privacyDismissed) {
        setPrivacyDismissed(false);
      }
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "图片上传失败");
    } finally {
      setUploading(false);
      setMenuOpen(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handlePaste(event: ClipboardEvent<HTMLFormElement>) {
    const imageFiles = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (!imageFiles.length) return;
    event.preventDefault();
    void handleFiles(imageFiles);
  }

  function handleDrop(event: DragEvent<HTMLFormElement>) {
    const imageFiles = Array.from(event.dataTransfer.files).filter((file) => file.type.startsWith("image/"));
    if (!imageFiles.length) return;
    event.preventDefault();
    void handleFiles(imageFiles);
  }

  function dismissPrivacyHint() {
    window.localStorage.setItem("finclaw.image_privacy_dismissed", "1");
    setPrivacyDismissed(true);
  }

  return (
    <form
      ref={formRef}
      className={[
        "composer",
        researchArmed ? "composer--research-armed" : "",
        expanded ? "composer--expanded" : "",
      ].filter(Boolean).join(" ")}
      onSubmit={submit}
      onPaste={handlePaste}
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleDrop}
    >
      {(attachments.length || referencedAttachments.length || uploadError || (!privacyDismissed && attachments.length)) ? (
        <div className="composer-attachments-panel">
          {attachments.length ? (
            <div className="composer-attachment-strip" aria-label="本轮上传图片">
              {attachments.map((item) => (
                <div className="composer-attachment-chip" key={item.attachment_id}>
                  <img src={apiUrl(item.thumb_url || item.view_url)} alt="待发送图片" />
                  <button
                    type="button"
                    onClick={() => setAttachments((prev) => prev.filter((candidate) => candidate.attachment_id !== item.attachment_id))}
                    aria-label="移除图片"
                    title="移除图片"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          ) : null}
          {referencedAttachments.length ? (
            <div className="composer-reference-strip" aria-label="引用图片">
              {referencedAttachments.map((item) => (
                <span className="composer-reference-chip" key={item.attachment_id}>
                  已引用图片
                  <button
                    type="button"
                    onClick={() => onRemoveReferencedAttachment(item.attachment_id)}
                    aria-label="取消引用图片"
                    title="取消引用"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          {!privacyDismissed && attachments.length ? (
            <div className="composer-privacy-hint">
              图片会随本轮消息发送给模型处理，请避免上传身份证、银行卡、完整账号等敏感内容。
              <button type="button" onClick={dismissPrivacyHint}>知道了</button>
            </div>
          ) : null}
          {uploadError ? <div className="composer-upload-error">{uploadError}</div> : null}
        </div>
      ) : null}
      <div className="composer-menu" ref={menuRef}>
        <button
          type="button"
          className="composer-plus"
          onClick={() => setMenuOpen((value) => !value)}
          disabled={inputDisabled || active || sendLocked}
          aria-label="打开输入能力菜单"
          aria-expanded={menuOpen}
          title="输入能力"
        >
          <PlusIcon />
        </button>
        {menuOpen ? (
          <div className="composer-menu-popover" role="menu">
            <button
              type="button"
              className={`composer-menu-item ${researchArmed ? "active" : ""}`}
              onClick={startResearch}
              role="menuitem"
            >
              <ResearchIcon />
              <span>{researchArmed ? "取消深度研究" : "本次使用深度研究"}</span>
            </button>
            <button
              type="button"
              className="composer-menu-item"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading || inputDisabled || attachments.length >= 4}
              role="menuitem"
            >
              <AttachmentIcon />
              <span>{uploading ? "正在上传图片" : "上传图片"}</span>
            </button>
          </div>
        ) : null}
      </div>
      <input
        ref={fileInputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        multiple
        hidden
        onChange={(event) => {
          if (event.currentTarget.files) void handleFiles(event.currentTarget.files);
        }}
      />
      {researchArmed ? (
        <span
          ref={modeRef}
          className="composer-mode-token"
          title="深度研究模式"
        >
          <span className="composer-mode-label">深度研究</span>
          <button
            type="button"
            className="composer-mode-remove"
            onClick={() => setResearchArmed(false)}
            aria-label="取消深度研究模式"
            title="取消深度研究"
          >
            ×
          </button>
        </span>
      ) : null}
      <textarea
        ref={textareaRef}
        value={input}
        onChange={(event) => setInput(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={researchArmed || hasImageContext ? "" : placeholder}
        disabled={inputDisabled}
        rows={1}
      />
      <div className="composer-actions" ref={actionsRef}>
        <button
          className="composer-send"
          disabled={sendLocked || uploading || (!text && !hasImageContext)}
          aria-label={active ? "发送新指令并中断当前生成" : researchArmed ? "发送深度研究" : "发送"}
          title={active ? "发送新指令" : researchArmed ? "发送深度研究" : "发送"}
        >
          <SendIcon />
        </button>
        {active ? (
          <button
            type="button"
            className="composer-stop-button"
            onClick={onStop}
            disabled={stopDisabled}
            aria-label="停止生成"
            title="停止生成"
          >
            <StopIcon />
          </button>
        ) : null}
      </div>
    </form>
  );
});

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M11 4h2v7h7v2h-7v7h-2v-7H4v-2h7V4Z" />
    </svg>
  );
}

function measureLongestLine(value: string, style: CSSStyleDeclaration): number {
  if (!value) return 0;
  const canvas = textMeasureCanvas ?? document.createElement("canvas");
  textMeasureCanvas = canvas;
  const context = canvas.getContext("2d");
  if (!context) return 0;
  context.font = style.font || [
    style.fontStyle || "normal",
    style.fontVariant || "normal",
    style.fontWeight || "400",
    style.fontSize || "15px",
    style.fontFamily || "sans-serif",
  ].join(" ");
  return value
    .split("\n")
    .reduce((max, line) => Math.max(max, context.measureText(line || " ").width), 0);
}

function getCompactInputWidth(
  form: HTMLFormElement | null,
  menu: HTMLDivElement | null,
  mode: HTMLElement | null,
  actions: HTMLDivElement | null,
  fallback: HTMLTextAreaElement,
): number {
  if (!form || !menu || !actions) return fallback.clientWidth;
  const style = window.getComputedStyle(form);
  const paddingLeft = Number.parseFloat(style.paddingLeft) || 0;
  const paddingRight = Number.parseFloat(style.paddingRight) || 0;
  const columnGap = Number.parseFloat(style.columnGap || style.gap) || 0;
  const available =
    form.clientWidth -
    paddingLeft -
    paddingRight -
    menu.offsetWidth -
    (mode?.offsetWidth ?? 0) -
    actions.offsetWidth -
    columnGap * (mode ? 3 : 2);
  return Math.max(80, available);
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3.8 20.2 21 12 3.8 3.8l1.7 7.1 8.1 1.1-8.1 1.1-1.7 7.1Z" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="7" y="7" width="10" height="10" rx="2.2" />
    </svg>
  );
}

function ResearchIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 19.2 9.2 15l2.5 2.5L18.8 10l-1.5-1.5-5.6 5.9-2.5-2.5L3.8 17.3 5 19.2Z" />
      <path d="M16.8 4.5h2.7v2.7h-2.7V4.5Zm-5.2 0h2.7v2.7h-2.7V4.5Zm-5.1 0h2.7v2.7H6.5V4.5Z" />
    </svg>
  );
}

function AttachmentIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8.2 18.2a5.1 5.1 0 0 1 0-7.2l5.8-5.8a3.7 3.7 0 0 1 5.2 5.2l-6.5 6.5a2.4 2.4 0 0 1-3.4-3.4l5.7-5.7 1.4 1.4-5.7 5.7a.4.4 0 0 0 .6.6l6.5-6.5a1.7 1.7 0 0 0-2.4-2.4l-5.8 5.8a3.1 3.1 0 1 0 4.4 4.4l5.4-5.4 1.4 1.4-5.4 5.4a5.1 5.1 0 0 1-7.2 0Z" />
    </svg>
  );
}
