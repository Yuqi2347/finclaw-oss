import { memo, useState } from "react";
import { apiUrl, collectReportLinks } from "../api/finclaw";
import type { AttachmentMeta, ChatMessage } from "../types";
import { MarkdownView } from "./MarkdownView";
import { ReportLinks } from "./ReportLinks";
import { SourceStrip } from "./SourceStrip";

type Props = {
  message: ChatMessage;
  onReferenceAttachment?: (message: ChatMessage, attachmentId: string) => void;
};

export const MessageBubble = memo(function MessageBubble({ message, onReferenceAttachment }: Props) {
  if (message.role === "assistant" && !hasVisibleAssistantContent(message)) return null;

  return (
    <div className={`message-row ${message.role}`}>
      <div className={`message-bubble ${message.role}`}>
        {message.role === "assistant" ? (
          <article className="reader-card">
            <SourceStrip sources={message.sources} />
            <div className="reader-body">
              <MarkdownView content={message.content} enableCitations={Boolean(message.sources?.length)} />
            </div>
          </article>
        ) : (
          <div className="user-message-content">
            {message.attachments?.length ? (
              <AttachmentGrid
                attachments={message.attachments}
                message={message}
                onReferenceAttachment={onReferenceAttachment}
              />
            ) : null}
            {message.content.trim() ? <span>{message.content}</span> : null}
          </div>
        )}
        {message.role === "assistant" && <ReportLinks toolCalls={message.toolCalls} links={message.reportLinks} />}
      </div>
    </div>
  );
}, areEqual);

function AttachmentGrid({
  attachments,
  message,
  onReferenceAttachment,
}: {
  attachments: AttachmentMeta[];
  message: ChatMessage;
  onReferenceAttachment?: (message: ChatMessage, attachmentId: string) => void;
}) {
  const [preview, setPreview] = useState<AttachmentMeta | null>(null);
  const images = attachments.filter((item) => item.type === "image");
  if (!images.length) return null;
  return (
    <>
      <div className="message-attachments">
        {images.map((item) => (
          <div className="message-attachment-thumb" key={item.attachment_id}>
            <button
              type="button"
              className="message-attachment-preview"
              onClick={() => setPreview(item)}
              aria-label="预览图片"
            >
              <img src={apiUrl(item.thumb_url || item.view_url)} alt="用户上传的图片" loading="lazy" />
            </button>
            <button
              type="button"
              className="message-attachment-reference"
              onClick={(event) => {
                event.stopPropagation();
                onReferenceAttachment?.(message, item.attachment_id);
              }}
              aria-label="引用此图"
              title="引用此图"
            >
              ↩
            </button>
            {item.referenced ? <span className="message-attachment-badge">引用</span> : null}
          </div>
        ))}
      </div>
      {preview ? (
        <div className="image-preview-backdrop" role="dialog" aria-modal="true" onClick={() => setPreview(null)}>
          <div className="image-preview-panel" onClick={(event) => event.stopPropagation()}>
            <button type="button" className="image-preview-close" onClick={() => setPreview(null)} aria-label="关闭预览">
              ×
            </button>
            <img src={apiUrl(preview.view_url || preview.thumb_url)} alt="图片预览" />
          </div>
        </div>
      ) : null}
    </>
  );
}

function hasVisibleAssistantContent(message: ChatMessage): boolean {
  if (message.content.trim()) return true;
  if (message.sources?.length) return true;
  if (message.reportLinks?.length) return true;
  if (message.toolCalls?.some((call) => collectReportLinks(call.result).length > 0)) return true;
  return false;
}

function areEqual(prev: Props, next: Props) {
  return prev.message === next.message
    || (
      prev.message.id === next.message.id
      && prev.message.role === next.message.role
      && prev.message.content === next.message.content
      && prev.message.attachments === next.message.attachments
      && prev.message.sources === next.message.sources
      && prev.message.toolCalls === next.message.toolCalls
      && prev.message.reportLinks === next.message.reportLinks
      && prev.onReferenceAttachment === next.onReferenceAttachment
    );
}
