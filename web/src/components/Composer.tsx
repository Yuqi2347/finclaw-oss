import { FormEvent, KeyboardEvent, memo, useEffect, useRef, useState } from "react";

type Props = {
  active: boolean;
  submitting: boolean;
  inputDisabled: boolean;
  submitDisabled: boolean;
  stopDisabled: boolean;
  onSubmit: (text: string) => void;
  onStop: () => void;
  onStartResearch: (text: string) => void;
};

export const Composer = memo(function Composer({
  active,
  submitting,
  inputDisabled,
  submitDisabled,
  stopDisabled,
  onSubmit,
  onStop,
  onStartResearch,
}: Props) {
  const [input, setInput] = useState("");
  const [researchArmed, setResearchArmed] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const text = input.trim();
  const preparingSubmit = submitting && !active;
  const sendLocked = preparingSubmit || submitDisabled;

  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
  }, [input]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (sendLocked) return;
    if (!text) return;
    const shouldStartResearch = researchArmed && !active;
    setInput("");
    setResearchArmed(false);
    if (shouldStartResearch) {
      onStartResearch(text);
      return;
    }
    onSubmit(text);
  }

  function startResearch() {
    if (active || sendLocked) return;
    setResearchArmed((value) => !value);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        ref={textareaRef}
        value={input}
        onChange={(event) => setInput(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask FinClaw..."
        disabled={inputDisabled}
        rows={1}
      />
      <div className="composer-toolbar">
        <div className="composer-tools">
          <button
            type="button"
            className={`composer-icon-button research-trigger ${researchArmed ? "armed" : ""}`}
            onClick={startResearch}
            disabled={active || sendLocked}
            title={researchArmed ? "本次将以深度研究发送" : "本次使用深度研究"}
            aria-label={researchArmed ? "取消本次深度研究" : "本次使用深度研究"}
            aria-pressed={researchArmed}
          >
            <ResearchIcon />
          </button>
          <button
            type="button"
            className="composer-icon-button"
            disabled
            title="附件能力预留"
            aria-label="附件能力预留"
          >
            <AttachmentIcon />
          </button>
        </div>
        <div className="composer-actions">
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
      </div>
    </form>
  );
});

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
