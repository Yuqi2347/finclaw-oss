import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelSessionRun,
  collectAnalysisJobs,
  collectReportLinks,
  createSession,
  deleteSession,
  getSessionApprovals,
  interruptSessionRun,
  listAnalysisJobs,
  listSessionMessages,
  listSessions,
  renameSession,
  streamChatWithSignal,
  streamConfirm,
} from "../api/finclaw";
import type { StreamHandlers } from "../api/finclaw";
import type { AnalysisJob, ChatMessage, PendingAction, SessionSummary, StoredChatMessage, ToolCallRecord, WebSource } from "../types";
import { Composer } from "./Composer";
import { MarketSidebar } from "./MarketSidebar";
import { MessageList } from "./MessageList";
import { PendingActionCard } from "./PendingActionCard";
import { UtilityRail } from "./UtilityRail";

function id() {
  return crypto.randomUUID();
}

const welcome: ChatMessage = {
  id: id(),
  role: "assistant",
  content: "你可以让我查看关注列表、持仓、最新报告，或在确认后运行市场主线/个股研究。",
};

const SESSION_ID_KEY = "finclaw.session_id";
const MEMORY_TARGET_LABELS: Record<string, string> = {
  profile: "用户画像",
  playbook: "研究框架",
  convictions: "当前投资判断",
};

function sessionId() {
  const existing = window.localStorage.getItem(SESSION_ID_KEY);
  if (existing) return existing;
  const created = crypto.randomUUID();
  window.localStorage.setItem(SESSION_ID_KEY, created);
  return created;
}

export function Chat() {
  const [session, setSession] = useState(sessionId);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([welcome]);
  const [messageListVersion, setMessageListVersion] = useState(0);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessage | null>(null);
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [stoppingRun, setStoppingRun] = useState(false);
  const [sessionLoading, setSessionLoading] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [activeMessageId, setActiveMessageId] = useState<string | null>(null);
  const [activeApproval, setActiveApproval] = useState<PendingAction | null>(null);
  const [queuedApprovals, setQueuedApprovals] = useState<PendingAction[]>([]);
  const [analysisJobs, setAnalysisJobs] = useState<AnalysisJob[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const lastServerMessageIdRef = useRef(0);
  const submitLockRef = useRef(false);
  const streamContentRef = useRef("");
  const streamMessageRef = useRef<ChatMessage | null>(null);
  const streamRafRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (streamRafRef.current != null) {
        window.cancelAnimationFrame(streamRafRef.current);
      }
    };
  }, []);

  const refreshSessions = useCallback(async () => {
    const rows = await listSessions();
    setSessions(rows);
    return rows;
  }, []);

  const refreshSessionMessages = useCallback(async () => {
    const rows = await listSessionMessages(session, lastServerMessageIdRef.current);
    if (rows.length) {
      lastServerMessageIdRef.current = Math.max(...rows.map((row) => row.message_id));
      setMessages((prev) => mergeServerMessages(prev, rows));
    }
  }, [session]);

  const refreshApprovals = useCallback(async () => {
    const state = await getSessionApprovals(session);
    setActiveApproval(state.active_action);
    setQueuedApprovals(state.queued_actions);
  }, [session]);

  function setCurrentStreamingMessage(patch: Partial<ChatMessage>) {
    const current = streamMessageRef.current;
    if (!current) return;
    const next = { ...current, ...patch };
    streamMessageRef.current = next;
    setStreamingMessage(next);
  }

  function flushStreamContent() {
    streamRafRef.current = null;
    setCurrentStreamingMessage({ content: streamContentRef.current });
  }

  function appendStreamText(text: string) {
    streamContentRef.current += text;
    if (streamRafRef.current == null) {
      streamRafRef.current = window.requestAnimationFrame(flushStreamContent);
    }
  }

  function finalizeStreamingMessage(fallbackContent?: string) {
    if (streamRafRef.current != null) {
      window.cancelAnimationFrame(streamRafRef.current);
      streamRafRef.current = null;
    }
    const current = streamMessageRef.current;
    if (!current) return;
    const content = streamContentRef.current || current.content || fallbackContent || "";
    const finalMessage = { ...current, content };
    streamMessageRef.current = null;
    streamContentRef.current = "";
    setStreamingMessage(null);
    if (isRenderableMessage(finalMessage)) {
      setMessages((prev) => [...prev, finalMessage]);
    }
  }

  function clearStreamingMessage() {
    if (streamRafRef.current != null) {
      window.cancelAnimationFrame(streamRafRef.current);
      streamRafRef.current = null;
    }
    streamMessageRef.current = null;
    streamContentRef.current = "";
    setStreamingMessage(null);
  }

  useEffect(() => {
    let cancelled = false;
    async function bootstrapSessions() {
      try {
        const rows = await refreshSessions();
        if (cancelled) return;
        const stored = window.localStorage.getItem(SESSION_ID_KEY);
        let target = rows.find((row) => row.session_id === stored)?.session_id;
        if (!target) {
          if (rows.length > 0) {
            target = rows[0].session_id;
          } else {
            const created = await createSession();
            if (cancelled) return;
            target = created.session_id;
            setSessions([created]);
          }
        }
        if (target) {
          window.localStorage.setItem(SESSION_ID_KEY, target);
          setSession(target);
        }
      } catch {
        if (!cancelled) {
          setSessionError("会话列表加载失败");
        }
      } finally {
        if (!cancelled) setSessionLoading(false);
      }
    }

    bootstrapSessions();

    listAnalysisJobs()
      .then((jobs) => setAnalysisJobs((prev) => mergeJobs(
        prev,
        jobs.filter((job) => ["running", "cancelling", "failed", "cancelled"].includes(job.status)),
      )))
      .catch(() => {
        // The chat should remain usable even if the background job list is unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [refreshSessions]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    setSessionLoading(true);
    setSessionError("");
    setStatus("");
    setActive(false);
    setActiveMessageId(null);
    setActiveApproval(null);
    setQueuedApprovals([]);
    clearStreamingMessage();
    lastServerMessageIdRef.current = 0;
    abortRef.current?.abort();

    async function loadSessionState() {
      try {
        const [rows, approvals, freshSessions] = await Promise.all([
          listSessionMessages(session, 0),
          getSessionApprovals(session),
          listSessions(),
        ]);
        if (cancelled) return;
        setSessions(freshSessions);
        lastServerMessageIdRef.current = rows.length ? Math.max(...rows.map((row) => row.message_id)) : 0;
        setMessages(rows.length ? mergeServerMessages([welcome], rows) : [welcome]);
        setMessageListVersion((value) => value + 1);
        setActiveApproval(approvals.active_action);
        setQueuedApprovals(approvals.queued_actions);
      } catch {
        if (!cancelled) {
          setMessages([welcome]);
          setMessageListVersion((value) => value + 1);
          setSessionError("当前会话加载失败");
        }
      } finally {
        if (!cancelled) setSessionLoading(false);
      }
    }

    loadSessionState();

    return () => {
      cancelled = true;
    };
  }, [session]);

  useEffect(() => {
    refreshApprovals().catch(() => {
      // Approval state is refreshed again after streaming events.
    });
  }, [refreshApprovals]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      try {
        if (!stopped) await refreshSessionMessages();
      } catch {
        // Message polling is best-effort; streaming remains the primary path.
      }
    };
    tick();
    const interval = window.setInterval(tick, 1800);
    return () => {
      stopped = true;
      window.clearInterval(interval);
    };
  }, [refreshSessionMessages]);

  async function runAssistantStream(streamer: (handlers: StreamHandlers, signal: AbortSignal) => Promise<void>) {
    const assistantId = id();
    const controller = new AbortController();
    const toolCalls: ToolCallRecord[] = [];
    let sources: WebSource[] = [];
    const initialMessage: ChatMessage = { id: assistantId, role: "assistant", content: "" };
    abortRef.current = controller;
    streamContentRef.current = "";
    streamMessageRef.current = initialMessage;
    setStreamingMessage(initialMessage);
    setActiveMessageId(assistantId);
    setActive(true);
    setStatus("Working");

    try {
      await streamer({
        onText: (text) => {
          appendStreamText(text);
          setStatus("Responding");
        },
        onStatus: setStatus,
        onPendingAction: (action) => {
          setActiveApproval((current) => current ?? action);
          refreshApprovals().catch(() => {
            // The SSE event already supplied the active action as fallback.
          });
        },
        onMemoryCandidate: (candidate) => {
          const rawTarget = candidate.target ? String(candidate.target) : "";
          const target = MEMORY_TARGET_LABELS[rawTarget] || rawTarget || "长期记忆";
          const message = candidate.message || `已生成 ${target} 候选，请在右栏长期记忆确认。`;
          appendStreamText(`${streamContentRef.current ? "\n\n" : ""}${message}`);
          window.dispatchEvent(new CustomEvent("finclaw:memory-candidate-created", { detail: candidate }));
        },
        onToolResult: (record) => {
          toolCalls.push(record);
          if (record.tool === "web_research") {
            sources = mergeSources(sources, collectWebSources(record.result));
          }
          const jobs = collectAnalysisJobs(record.result);
          if (jobs.length) {
            setAnalysisJobs((prev) => mergeJobs(prev, jobs));
          }
          setCurrentStreamingMessage({ toolCalls: [...toolCalls], sources: [...sources] });
        },
        onDone: () => {
          setStatus("Done");
        },
        onError: (message) => {
          if (isSessionBusyError(message)) {
            setStatus("上一轮正在停止，请稍后再试");
            return;
          }
          if (!streamContentRef.current) {
            appendStreamText(message);
          }
          setStatus("Error");
        },
      }, controller.signal);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        if (!streamContentRef.current) {
          appendStreamText(String(error));
        }
      }
    }

    finalizeStreamingMessage();
    setActive(false);
    setActiveMessageId(null);
    setStatus("");
    abortRef.current = null;
    await refreshApprovals().catch(() => {
      // Keep the chat usable even if approval polling fails once.
    });
  }

  async function handleComposerSubmit(text: string) {
    if (stoppingRun) return;
    if (active) {
      setMessages((prev) => [...prev, { id: id(), role: "user", content: text }]);
      setStatus("已收到新指令，正在切换");
      await interruptSessionRun(session, text);
      abortRef.current?.abort();
      await refreshSessionMessages().catch(() => {
        // The periodic poll will retry.
      });
      return;
    }

    if (submitLockRef.current) return;
    submitLockRef.current = true;
    setSubmitting(true);
    try {
      if (!text) return;
      setMessages((prev) => [...prev, { id: id(), role: "user", content: text }]);
      await runAssistantStream((handlers, signal) => streamChatWithSignal(text, handlers, signal, session));
    } finally {
      submitLockRef.current = false;
      setSubmitting(false);
    }
  }

  async function handleStopGeneration() {
    if (!active) return;
    const controller = abortRef.current;
    setStoppingRun(true);
    setStatus("Stopping");
    controller?.abort();
    const cancelPromise = cancelSessionRun(session).catch(() => {
      // Local abort should release the UI even if the backend cancel request fails.
    });
    try {
      await Promise.all([cancelPromise, waitForSessionRunRelease(session)]);
      setStatus("Stopped");
    } finally {
      setStoppingRun(false);
    }
  }

  async function startResearchFromInput(text: string) {
    if (active || submitting || stoppingRun || sessionLoading) return;
    if (!text) return;
    setSubmitting(true);
    setStatus("正在生成研究确认卡片");
    try {
      setMessages((prev) => [...prev, { id: id(), role: "user", content: `开启研究：${text}` }]);
      const researchPrompt = [
        "请根据用户研究需求直接调用 start_research_thread；由你提炼 subject、research_goal、constraints 和工具权限，不要输出启动说明。",
        `用户研究需求：${text}`,
      ].join("\n");
      await runAssistantStream((handlers, signal) => streamChatWithSignal(researchPrompt, handlers, signal, session, "deep_research"));
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : "研究确认卡片创建失败");
    } finally {
      setSubmitting(false);
      setStatus("");
    }
  }

  async function switchSession(nextSessionId: string) {
    if (!nextSessionId || nextSessionId === session || active) return;
    window.localStorage.setItem(SESSION_ID_KEY, nextSessionId);
    setSession(nextSessionId);
  }

  async function handleNewSession() {
    if (active) return;
    setSessionLoading(true);
    try {
      const created = await createSession();
      setSessions((prev) => [created, ...prev.filter((row) => row.session_id !== created.session_id)]);
      window.localStorage.setItem(SESSION_ID_KEY, created.session_id);
      setSession(created.session_id);
    } finally {
      setSessionLoading(false);
    }
  }

  async function handleDeleteSession(sessionToDelete: string) {
    if (active) return;
    const current = sessions.find((row) => row.session_id === sessionToDelete);
    const label = current?.title || sessionToDelete;
    if (!window.confirm(`删除会话「${label}」？此操作不可撤销。`)) return;
    await deleteSession(sessionToDelete);
    const nextSessions = sessions.filter((row) => row.session_id !== sessionToDelete);
    setSessions(nextSessions);
    if (session === sessionToDelete) {
      const next = nextSessions[0];
      if (next) {
        window.localStorage.setItem(SESSION_ID_KEY, next.session_id);
        setSession(next.session_id);
      } else {
        const created = await createSession();
        setSessions([created]);
        window.localStorage.setItem(SESSION_ID_KEY, created.session_id);
        setSession(created.session_id);
      }
    }
  }

  async function handleRenameSession() {
    if (active || sessionLoading) return;
    const current = sessions.find((row) => row.session_id === session);
    const currentTitle = current?.title || "";
    const nextTitle = window.prompt("输入新的会话名称", currentTitle);
    if (nextTitle === null) return;
    const trimmed = nextTitle.trim();
    if (!trimmed || trimmed === currentTitle) return;
    try {
      const updated = await renameSession(session, trimmed);
      setSessions((prev) => prev.map((row) => (row.session_id === updated.session_id ? updated : row)));
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : "会话重命名失败");
    }
  }

  async function onConfirm(actionId: string, approved: boolean, argumentsOverride?: Record<string, unknown>) {
    if (active) return;
    if (!approved) {
      const controller = new AbortController();
      let cancelText = "";
      abortRef.current = controller;
      setActive(true);
      setStatus("处理中");
      try {
        await streamConfirm(actionId, approved, {
          onText: (text) => {
            cancelText += text;
          },
          onStatus: setStatus,
          onPendingAction: (action) => setActiveApproval(action),
          onMemoryCandidate: (candidate) => {
            window.dispatchEvent(new CustomEvent("finclaw:memory-candidate-created", { detail: candidate }));
          },
          onToolResult: (record) => {
            const jobs = collectAnalysisJobs(record.result);
            if (jobs.length) {
              setAnalysisJobs((prev) => mergeJobs(prev, jobs));
            }
          },
          onDone: () => setStatus("Done"),
          onError: (message) => setStatus(message),
        }, argumentsOverride);
        setMessages((prev) => [...prev, { id: id(), role: "assistant", content: cancelText.trim() || "已取消本次操作。" }]);
      } finally {
        setActive(false);
        setActiveMessageId(null);
        setStatus("");
        abortRef.current = null;
        await refreshApprovals().catch(() => {});
      }
      return;
    }
    await runAssistantStream((handlers) => streamConfirm(actionId, approved, handlers, argumentsOverride));
    await refreshApprovals().catch(() => {
      // Periodic interaction will recover.
    });
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <div className="brand">FinClaw</div>
        </div>
        <div className="session-bar">
          <a className="session-link" href="/logs" target="_blank" rel="noreferrer">
            LLM Logs
          </a>
          <select
            value={session}
            onChange={(event) => void switchSession(event.target.value)}
            disabled={active || sessionLoading}
            aria-label="切换会话"
          >
            {sessions.map((item) => (
              <option key={item.session_id} value={item.session_id}>
                {item.title}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => void handleNewSession()} disabled={active || sessionLoading}>
            新会话
          </button>
          <button type="button" className="ghost" onClick={() => void handleRenameSession()} disabled={active || sessionLoading}>
            重命名
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => void handleDeleteSession(session)}
            disabled={active || sessionLoading}
          >
            删除
          </button>
        </div>
      </header>
      {sessionError ? <div className="session-error">{sessionError}</div> : null}

      <div className="workspace">
        <MarketSidebar />
        <section className="conversation-panel">
          <MessageList
            listKey={`${session}:${messageListVersion}`}
            messages={messages}
            streamingMessage={streamingMessage}
            active={active}
            status={status}
            activeMessageId={activeMessageId}
            loading={sessionLoading}
          />

          <div className="composer-column">
            {activeApproval ? (
              <div className="composer approval-composer">
                <div className="approval-composer-body">
                  <div className="approval-kicker">等待确认</div>
                  <PendingActionCard key={activeApproval.action_id} action={activeApproval} onConfirm={onConfirm} />
                  {queuedApprovals.length > 0 && (
                    <div className="approval-queue-note">还有 {queuedApprovals.length} 个操作排队，将在当前操作处理后依次确认。</div>
                  )}
                </div>
              </div>
            ) : (
              <Composer
                active={active}
                submitting={submitting}
                inputDisabled={sessionLoading}
                submitDisabled={sessionLoading || stoppingRun}
                stopDisabled={sessionLoading || stoppingRun}
                onSubmit={(text) => void handleComposerSubmit(text)}
                onStop={() => void handleStopGeneration()}
                onStartResearch={(text) => void startResearchFromInput(text)}
              />
            )}
          </div>
        </section>
        <UtilityRail
          session={session}
          sessions={sessions}
          jobs={analysisJobs}
          onJobUpdate={(job) => {
            setAnalysisJobs((prev) => mergeJobs(prev, [job]));
            if (job.status === "completed" || job.status === "failed") {
              refreshSessionMessages().catch(() => {
                // The periodic poll will retry.
              });
            }
          }}
          onJobDismiss={(jobId) => setAnalysisJobs((prev) => prev.filter((job) => job.job_id !== jobId))}
          active={active}
          status={status}
          sessionError={sessionError}
        />
      </div>
    </main>
  );
}

async function waitForSessionRunRelease(sessionId: string, timeoutMs = 10000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const sessions = await listSessions().catch(() => []);
    const current = sessions.find((item) => item.session_id === sessionId);
    if (!current?.active_run_id) return;
    await delay(250);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function isSessionBusyError(message: string): boolean {
  return message.includes("当前会话正在生成回答");
}

function isRenderableMessage(message: ChatMessage): boolean {
  if (message.content.trim()) return true;
  if (message.sources?.length) return true;
  if (message.reportLinks?.length) return true;
  if (message.toolCalls?.some((call) => collectReportLinks(call.result).length > 0)) return true;
  return false;
}

function mergeJobs(current: AnalysisJob[], incoming: AnalysisJob[]): AnalysisJob[] {
  const map = new Map(current.map((job) => [job.job_id, job]));
  for (const job of incoming) {
    map.set(job.job_id, job);
  }
  return Array.from(map.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
}

function mergeServerMessages(current: ChatMessage[], incoming: StoredChatMessage[]): ChatMessage[] {
  const serverIds = new Set(current.map((msg) => msg.serverId).filter((value): value is number => typeof value === "number"));
  const additions = incoming
    .filter((row) => !serverIds.has(row.message_id))
    .filter((row) => !current.some((msg) => msg.role === row.role && msg.content.trim() === row.content.trim()))
    .filter((row) => row.content.trim())
    .map((row) => ({
      id: `server-${row.message_id}`,
      serverId: row.message_id,
      role: row.role,
      content: row.content,
      toolCalls: row.tool_calls,
      reportLinks: row.report_links,
      sources: row.sources ?? [],
      citations: row.citations ?? [],
    }));
  if (!additions.length) return current;
  return [...current, ...additions];
}

function collectWebSources(payload: unknown): WebSource[] {
  if (!payload || typeof payload !== "object") return [];
  const obj = payload as Record<string, unknown>;
  const nested = obj.result && typeof obj.result === "object" ? obj.result as Record<string, unknown> : null;
  const sourceOwner = Array.isArray(obj.sources) ? obj : nested;
  const rawSources = sourceOwner && Array.isArray(sourceOwner.sources) ? sourceOwner.sources : [];
  return rawSources
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .filter((item) => typeof item.url === "string" && item.url.length > 0)
    .map((item, index) => ({
      source_id: typeof item.source_id === "string" ? item.source_id : `src_${index + 1}`,
      marker: typeof item.marker === "number" ? item.marker : index + 1,
      title: typeof item.title === "string" ? item.title : String(item.url),
      url: String(item.url),
      domain: typeof item.domain === "string" ? item.domain : undefined,
      published_at: typeof item.published_at === "string" ? item.published_at : null,
      credibility: typeof item.credibility === "string" ? item.credibility : undefined,
      excerpt: typeof item.excerpt === "string" ? item.excerpt : undefined,
      provider: typeof item.provider === "string" ? item.provider : undefined,
    }));
}

function mergeSources(current: WebSource[], incoming: WebSource[]): WebSource[] {
  const seen = new Set(current.map((source) => source.url));
  const merged = [...current];
  for (const source of incoming) {
    if (!source.url || seen.has(source.url)) continue;
    seen.add(source.url);
    merged.push({ ...source, marker: merged.length + 1 });
  }
  return merged;
}
