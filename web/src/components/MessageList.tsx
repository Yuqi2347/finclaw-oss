import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import { Virtuoso, VirtuosoHandle } from "react-virtuoso";
import { collectReportLinks } from "../api/finclaw";
import type { ChatMessage } from "../types";
import { AgentStatus } from "./AgentStatus";
import { MessageBubble } from "./MessageBubble";

type Props = {
  messages: ChatMessage[];
  streamingMessage: ChatMessage | null;
  active: boolean;
  status: string;
  activeMessageId: string | null;
  loading?: boolean;
  listKey: string;
  onReferenceAttachment?: (message: ChatMessage, attachmentId: string) => void;
};

export const MessageList = memo(function MessageList({
  messages,
  streamingMessage,
  active,
  status,
  activeMessageId,
  loading,
  listKey,
  onReferenceAttachment,
}: Props) {
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  const initialPositioningRef = useRef(false);
  const positionedListKeyRef = useRef<string | null>(null);
  const settleTimerRef = useRef<number | null>(null);
  const maxPositioningTimerRef = useRef<number | null>(null);
  const items = useMemo(
    () => {
      const visibleMessages = messages.filter(hasVisibleMessage);
      if (!streamingMessage) return visibleMessages;
      return [...visibleMessages, streamingMessage];
    },
    [messages, streamingMessage],
  );

  const scrollToLatest = useCallback(() => {
    if (!items.length) return;
    virtuosoRef.current?.scrollToIndex({
      index: items.length - 1,
      align: "end",
      behavior: "auto",
    });
  }, [items.length]);

  const finishInitialPositioning = useCallback(() => {
    scrollToLatest();
    initialPositioningRef.current = false;
    if (settleTimerRef.current != null) {
      window.clearTimeout(settleTimerRef.current);
      settleTimerRef.current = null;
    }
    if (maxPositioningTimerRef.current != null) {
      window.clearTimeout(maxPositioningTimerRef.current);
      maxPositioningTimerRef.current = null;
    }
  }, [scrollToLatest]);

  useEffect(() => {
    if (loading || !items.length) return;
    if (positionedListKeyRef.current === listKey) return;
    positionedListKeyRef.current = listKey;
    initialPositioningRef.current = true;
    const raf = window.requestAnimationFrame(scrollToLatest);
    maxPositioningTimerRef.current = window.setTimeout(finishInitialPositioning, 2500);
    return () => {
      window.cancelAnimationFrame(raf);
      if (settleTimerRef.current != null) {
        window.clearTimeout(settleTimerRef.current);
        settleTimerRef.current = null;
      }
      if (maxPositioningTimerRef.current != null) {
        window.clearTimeout(maxPositioningTimerRef.current);
        maxPositioningTimerRef.current = null;
      }
      initialPositioningRef.current = false;
    };
  }, [finishInitialPositioning, items.length, listKey, loading, scrollToLatest]);

  const handleTotalListHeightChanged = useCallback(() => {
    if (!initialPositioningRef.current) return;
    scrollToLatest();
    if (settleTimerRef.current != null) {
      window.clearTimeout(settleTimerRef.current);
    }
    settleTimerRef.current = window.setTimeout(finishInitialPositioning, 180);
  }, [finishInitialPositioning, scrollToLatest]);

  return (
    <Virtuoso
      ref={virtuosoRef}
      key={listKey}
      className="chat-panel"
      data={items}
      computeItemKey={(_, message) => message.id}
      followOutput="smooth"
      increaseViewportBy={{ top: 900, bottom: 900 }}
      totalListHeightChanged={handleTotalListHeightChanged}
      itemContent={(_, message) => (
        <div className="message-virtual-item">
          <div className="reader-column">
            {active && activeMessageId === message.id && (
              <AgentStatus active={active} status={status} />
            )}
            <MessageBubble message={message} onReferenceAttachment={onReferenceAttachment} />
          </div>
        </div>
      )}
    />
  );
});

function hasVisibleMessage(message: ChatMessage): boolean {
  if (message.role === "user") return message.content.trim().length > 0 || Boolean(message.attachments?.length);
  if (message.attachments?.length) return true;
  if (message.content.trim()) return true;
  if (message.sources?.length) return true;
  if (message.reportLinks?.length) return true;
  if (message.toolCalls?.some((call) => collectReportLinks(call.result).length > 0)) return true;
  return false;
}
