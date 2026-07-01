import { useEffect, useMemo, useState } from "react";
import { clearLlmLogs, getLlmLog, listLlmLogs } from "../api/finclaw";
import type { LlmLogDetail, LlmLogSummary } from "../types";

type DetailTab = "request" | "response" | "tools" | "metadata";

export function LlmLogsPage() {
  const [logs, setLogs] = useState<LlmLogSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<LlmLogDetail | null>(null);
  const [tab, setTab] = useState<DetailTab>("request");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  const filteredLogs = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return logs;
    return logs.filter((log) => [
      log.model,
      log.status,
      log.session_id,
      log.run_id,
      log.error,
      String(log.id),
    ].some((value) => String(value ?? "").toLowerCase().includes(needle)));
  }, [logs, query]);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const rows = await listLlmLogs(100);
      setLogs(rows);
      setSelectedId((current) => {
        if (current && rows.some((row) => row.id === current)) return current;
        return rows[0]?.id ?? null;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "日志加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    getLlmLog(selectedId)
      .then((item) => {
        if (!cancelled) setDetail(item);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "日志详情加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  async function handleClear() {
    if (!window.confirm("确定清空所有 LLM 调用日志？")) return;
    await clearLlmLogs();
    setLogs([]);
    setSelectedId(null);
    setDetail(null);
  }

  return (
    <main className="logs-page">
      <header className="logs-topbar">
        <div>
          <div className="logs-eyebrow">Observability</div>
          <h1>LLM 调用日志</h1>
          <p>逐条查看每次模型调用的完整 prompt、tools、返回内容、耗时和错误。</p>
        </div>
        <div className="logs-actions">
          <a className="logs-link" href="/">返回对话</a>
          <button onClick={refresh} disabled={loading}>{loading ? "刷新中" : "刷新"}</button>
          <button className="danger" onClick={handleClear}>清空</button>
        </div>
      </header>

      {error ? <div className="logs-error">{error}</div> : null}

      <section className="logs-workspace">
        <aside className="logs-list">
          <div className="logs-search">
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 id / session / status / model" />
          </div>
          <div className="logs-list-scroll">
            {filteredLogs.map((log) => (
              <button
                key={log.id}
                className={`log-row ${selectedId === log.id ? "active" : ""}`}
                onClick={() => setSelectedId(log.id)}
              >
                <div className="log-row-head">
                  <span>#{log.id}</span>
                  <StatusPill status={log.status} />
                </div>
                <div className="log-model">{log.model}</div>
                <div className="log-meta">
                  <span>{formatDate(log.started_at)}</span>
                  <span>{formatMs(log.duration_ms)}</span>
                </div>
                <div className="log-token-row">
                  <span>In ~{formatNumber(log.request_tokens_estimate)}</span>
                  <span>Out ~{formatNumber(log.response_tokens_estimate)}</span>
                  <strong>Total ~{formatNumber(log.total_tokens_estimate)}</strong>
                </div>
                {log.error ? <div className="log-error-line">{log.error}</div> : null}
              </button>
            ))}
            {!filteredLogs.length ? <div className="logs-empty">暂无 LLM 调用日志</div> : null}
          </div>
        </aside>

        <section className="logs-detail">
          {detail ? (
            <>
              <div className="detail-head">
                <div>
                  <h2>调用 #{detail.id}</h2>
                  <p>
                    {detail.model} · {formatDate(detail.started_at)} · {formatMs(detail.duration_ms)}
                    {" · "}In ~{formatNumber(detail.request_tokens_estimate)}
                    {" · "}Out ~{formatNumber(detail.response_tokens_estimate)}
                  </p>
                </div>
                <StatusPill status={detail.status} />
              </div>

              <div className="detail-tabs">
                {(["request", "response", "tools", "metadata"] as DetailTab[]).map((item) => (
                  <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
                    {tabLabel(item)}
                  </button>
                ))}
              </div>

              <div className="detail-body">
                {tab === "request" ? <RequestView request={detail.request} /> : null}
                {tab === "response" ? <ResponseView response={detail.response} error={detail.error} /> : null}
                {tab === "tools" ? <ToolsView request={detail.request} response={detail.response} /> : null}
                {tab === "metadata" ? <JsonBlock value={metadataOf(detail)} /> : null}
              </div>
            </>
          ) : (
            <div className="logs-empty detail-empty">选择一条日志查看详情</div>
          )}
        </section>
      </section>
    </main>
  );
}

function RequestView({ request }: { request?: Record<string, unknown> }) {
  const messages = Array.isArray(request?.messages) ? request.messages as Array<Record<string, unknown>> : [];
  return (
    <div className="request-view">
      <SectionTitle title="Messages" count={messages.length} />
      <div className="message-stack">
        {messages.map((message, index) => (
          <MessageCard key={index} message={message} index={index} />
        ))}
      </div>
      <SectionTitle title="Raw Payload" />
      <JsonBlock value={request ?? {}} />
    </div>
  );
}

function ResponseView({ response, error }: { response?: Record<string, unknown>; error?: string | null }) {
  return (
    <div className="response-view">
      {error ? <div className="logs-error response-error">{error}</div> : null}
      <SectionTitle title="Content" />
      <TextBlock value={String(response?.content ?? "") || "(empty)"} />
      {response?.reasoning_content ? (
        <>
          <SectionTitle title="Reasoning Content" />
          <TextBlock value={String(response.reasoning_content)} />
        </>
      ) : null}
      <SectionTitle title="Raw Response" />
      <JsonBlock value={response ?? {}} />
    </div>
  );
}

function ToolsView({ request, response }: { request?: Record<string, unknown>; response?: Record<string, unknown> }) {
  return (
    <div>
      <SectionTitle title="Available Tools" count={Array.isArray(request?.tools) ? request.tools.length : 0} />
      <JsonBlock value={request?.tools ?? []} />
      <SectionTitle title="Returned Tool Calls" count={Array.isArray(response?.tool_calls) ? response.tool_calls.length : 0} />
      <JsonBlock value={response?.tool_calls ?? []} />
    </div>
  );
}

function MessageCard({ message, index }: { message: Record<string, unknown>; index: number }) {
  const content = message.content;
  return (
    <article className="log-message-card">
      <div className="log-message-head">
        <span>#{index + 1}</span>
        <strong>{String(message.role ?? "unknown")}</strong>
      </div>
      {typeof content === "string" ? <TextBlock value={content || "(empty)"} /> : <JsonBlock value={content ?? message} />}
      {message.tool_calls ? (
        <>
          <div className="inline-label">tool_calls</div>
          <JsonBlock value={message.tool_calls} />
        </>
      ) : null}
    </article>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2);
  return (
    <div className="code-wrap">
      <button onClick={() => navigator.clipboard?.writeText(text)}>复制</button>
      <pre>{text}</pre>
    </div>
  );
}

function TextBlock({ value }: { value: string }) {
  return (
    <div className="text-wrap">
      <button onClick={() => navigator.clipboard?.writeText(value)}>复制</button>
      <pre>{value}</pre>
    </div>
  );
}

function SectionTitle({ title, count }: { title: string; count?: number }) {
  return <h3 className="section-title">{title}{typeof count === "number" ? <span>{count}</span> : null}</h3>;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill ${status}`}>{status}</span>;
}

function formatDate(value?: string | null) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

function formatMs(value?: number | null) {
  if (value == null) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${value}ms`;
}

function formatNumber(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "--";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`;
  return String(Math.round(value));
}

function tabLabel(tab: DetailTab) {
  if (tab === "request") return "Prompt";
  if (tab === "response") return "返回";
  if (tab === "tools") return "Tools";
  return "元信息";
}

function metadataOf(detail: LlmLogDetail) {
  const { request, response, ...metadata } = detail;
  return metadata;
}
