import { FormEvent, KeyboardEvent, memo, useEffect, useLayoutEffect, useRef, useState } from "react";

type Props = {
  active: boolean;
  submitting: boolean;
  inputDisabled: boolean;
  submitDisabled: boolean;
  stopDisabled: boolean;
  onSubmit: (text: string) => void;
  onStop: () => void;
  onStartResearch: (text: string) => void;
  placeholder?: string;
};

let textMeasureCanvas: HTMLCanvasElement | undefined;

export const Composer = memo(function Composer({
  active,
  submitting,
  inputDisabled,
  submitDisabled,
  stopDisabled,
  onSubmit,
  onStop,
  onStartResearch,
  placeholder = "Ask FinClaw...",
}: Props) {
  const [input, setInput] = useState("");
  const [researchArmed, setResearchArmed] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const formRef = useRef<HTMLFormElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const modeRef = useRef<HTMLSpanElement | null>(null);
  const actionsRef = useRef<HTMLDivElement | null>(null);
  const text = input.trim();
  const preparingSubmit = submitting && !active;
  const sendLocked = preparingSubmit || submitDisabled;

  useLayoutEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    const computed = window.getComputedStyle(node);
    const longestLineWidth = measureLongestLine(input, computed);
    const compactInputWidth = getCompactInputWidth(formRef.current, menuRef.current, modeRef.current, actionsRef.current, node);
    const exceedsCompactLine = Boolean(input.trim()) && longestLineWidth > compactInputWidth;
    const nextExpanded = input.includes("\n") || exceedsCompactLine;
    setExpanded(nextExpanded);
    node.style.height = `${Math.min(node.scrollHeight, nextExpanded ? 210 : 38)}px`;
  }, [input, expanded]);

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
    if (sendLocked) return;
    if (!text) return;
    const shouldStartResearch = researchArmed && !active;
    setInput("");
    setExpanded(false);
    setResearchArmed(false);
    setMenuOpen(false);
    if (shouldStartResearch) {
      onStartResearch(text);
      return;
    }
    onSubmit(text);
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

  return (
    <form
      ref={formRef}
      className={[
        "composer",
        researchArmed ? "composer--research-armed" : "",
        expanded ? "composer--expanded" : "",
      ].filter(Boolean).join(" ")}
      onSubmit={submit}
    >
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
            <button type="button" className="composer-menu-item" disabled role="menuitem">
              <AttachmentIcon />
              <span>附件能力预留</span>
            </button>
          </div>
        ) : null}
      </div>
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
        placeholder={researchArmed ? "" : placeholder}
        disabled={inputDisabled}
        rows={1}
      />
      <div className="composer-actions" ref={actionsRef}>
        <button
          className="composer-send"
          disabled={sendLocked || !text}
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
