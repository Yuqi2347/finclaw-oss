import { memo } from "react";
import { collectReportLinks } from "../api/finclaw";
import type { ChatMessage } from "../types";
import { MarkdownView } from "./MarkdownView";
import { ReportLinks } from "./ReportLinks";
import { SourceStrip } from "./SourceStrip";

type Props = {
  message: ChatMessage;
};

export const MessageBubble = memo(function MessageBubble({ message }: Props) {
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
          <span>{message.content}</span>
        )}
        {message.role === "assistant" && <ReportLinks toolCalls={message.toolCalls} links={message.reportLinks} />}
      </div>
    </div>
  );
}, areEqual);

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
      && prev.message.sources === next.message.sources
      && prev.message.toolCalls === next.message.toolCalls
      && prev.message.reportLinks === next.message.reportLinks
    );
}
