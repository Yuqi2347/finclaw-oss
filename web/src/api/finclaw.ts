import type {
  AnalysisJob,
  ApprovalQueue,
  CapabilityModule,
  DashboardSidebarPayload,
  LlmLogDetail,
  LlmLogSummary,
  PendingAction,
  ResearchRecordDetail,
  ResearchRecordSummary,
  ResearchThread,
  SessionSummary,
  StoredChatMessage,
  ToolCallRecord,
} from "../types";

export const API_BASE = import.meta.env.VITE_FINCLAW_API_BASE ?? "http://127.0.0.1:8800";
const DASHBOARD_REQUEST_TIMEOUT_MS = 25000;

export type StreamHandlers = {
  onText: (text: string) => void;
  onStatus: (status: string) => void;
  onPendingAction: (action: PendingAction) => void;
  onMemoryCandidate?: (candidate: { candidate_id?: string; target?: string; message?: string }) => void;
  onToolResult: (record: ToolCallRecord) => void;
  onDone: () => void;
  onError: (message: string) => void;
};

export type RefreshStreamHandlers = {
  onSidebarData: (data: DashboardSidebarPayload) => void;
  onRefreshStarted: (data: { targets: string[]; total: number; started_at: string }) => void;
  onRefreshProgress: (data: {
    stage: string;
    ticker?: string;
    progress?: string;
    percentage?: number;
    status: string;
    message?: string;
    error?: string;
  }) => void;
  onRefreshCompleted: (data: { refreshed: string[]; errors: string[]; completed_at: string }) => void;
  onRefreshFailed: (data: { error: string; errors: string[] }) => void;
  onRefreshWarning: (data: { message: string }) => void;
  onError: (message: string) => void;
};

export async function streamChat(message: string, handlers: StreamHandlers): Promise<void> {
  await streamPost("/api/chat/stream", { message }, handlers);
}

export async function streamChatWithSignal(
  message: string,
  handlers: StreamHandlers,
  signal: AbortSignal,
  sessionId = "default",
  mode?: string,
): Promise<void> {
  await streamPost("/api/chat/stream", { message, session_id: sessionId, mode }, handlers, signal);
}

export async function streamConfirm(
  actionId: string,
  approved: boolean,
  handlers: StreamHandlers,
  argumentsOverride?: Record<string, unknown>,
): Promise<void> {
  await streamPost(`/api/actions/${actionId}/confirm/stream`, { approved, arguments: argumentsOverride }, handlers);
}

async function streamPost(
  path: string,
  body: unknown,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return;
    throw error;
  }

  if (!response.ok || !response.body) {
    handlers.onError(`请求失败：${response.status} ${response.statusText}`);
    throw new Error(`请求失败：${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      throw error;
    }
    const { value, done } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      parseSseBlock(part, handlers);
    }
  }
  if (buffer.trim()) parseSseBlock(buffer, handlers);
}

function parseSseBlock(block: string, handlers: StreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const raw of block.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  const payload = JSON.parse(dataLines.join("\n"));

  if (event === "text_delta") handlers.onText(payload.text ?? "");
  if (event === "status_delta") handlers.onStatus(payload.message ?? payload.phase ?? "");
  if (event === "tool_call_start") handlers.onStatus(`正在调用工具 ${payload.name ?? "tool"}`);
  if (event === "tool_call_result") {
    handlers.onStatus(`工具 ${payload.name ?? "tool"} 已完成`);
    handlers.onToolResult({ tool: payload.name, result: payload.result });
  }
  if (event === "approval_required") {
    handlers.onStatus("等待确认");
    handlers.onPendingAction(payload.action);
  }
  if (event === "memory_candidate_created") {
    handlers.onStatus("候选记忆待确认");
    handlers.onMemoryCandidate?.(payload);
  }
  if (event === "message_done") handlers.onDone();
  if (event === "error") handlers.onError(payload.message ?? "未知错误");
}

export async function cancelSessionRun(sessionId: string): Promise<void> {
  await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
  });
}

export async function interruptSessionRun(sessionId: string, message: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/interrupt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) throw new Error(`interrupt failed: ${response.status}`);
}

export function collectReportLinks(payload: unknown): Array<{ title: string; view_url: string; download_url: string }> {
  const links: Array<{ title: string; view_url: string; download_url: string }> = [];
  walk(payload);
  return links;

  function walk(value: unknown): void {
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (!value || typeof value !== "object") return;
    const obj = value as Record<string, unknown>;
    if (typeof obj.view_url === "string" && typeof obj.download_url === "string") {
      const meta = (obj.meta ?? {}) as Record<string, unknown>;
      links.push({
        title: typeof meta.title === "string" ? meta.title : "报告",
        view_url: obj.view_url,
        download_url: obj.download_url,
      });
    }
    Object.values(obj).forEach(walk);
  }
}

export function collectAnalysisJobs(payload: unknown): AnalysisJob[] {
  const jobs: AnalysisJob[] = [];
  walk(payload);
  return jobs;

  function walk(value: unknown): void {
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (!value || typeof value !== "object") return;
    const obj = value as Record<string, unknown>;
    if (obj.job && typeof obj.job === "object") {
      jobs.push(obj.job as AnalysisJob);
    }
    Object.values(obj).forEach(walk);
  }
}

export async function getAnalysisJob(jobId: string): Promise<AnalysisJob> {
  const response = await fetch(`${API_BASE}/api/analysis/jobs/${jobId}`);
  if (!response.ok) throw new Error(`job status failed: ${response.status}`);
  return response.json();
}

export async function cancelAnalysisJob(jobId: string): Promise<AnalysisJob> {
  const response = await fetch(`${API_BASE}/api/analysis/jobs/${jobId}/cancel`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(`job cancel failed: ${response.status}`);
  return response.json();
}

export async function listAnalysisJobs(): Promise<AnalysisJob[]> {
  const response = await fetch(`${API_BASE}/api/analysis/jobs`);
  if (!response.ok) throw new Error(`job list failed: ${response.status}`);
  return response.json();
}

export async function listSessionMessages(sessionId: string, afterId: number): Promise<StoredChatMessage[]> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/messages?after_id=${afterId}`);
  if (!response.ok) throw new Error(`session messages failed: ${response.status}`);
  return response.json();
}

export async function getSessionApprovals(sessionId: string): Promise<ApprovalQueue> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/approvals`);
  if (!response.ok) throw new Error(`session approvals failed: ${response.status}`);
  return response.json();
}

export async function listSessions(limit = 50): Promise<SessionSummary[]> {
  const response = await fetch(`${API_BASE}/api/sessions?limit=${limit}`);
  if (!response.ok) throw new Error(`session list failed: ${response.status}`);
  const payload = await response.json();
  return payload.sessions ?? [];
}

export async function createSession(title?: string): Promise<SessionSummary> {
  const response = await fetch(`${API_BASE}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) throw new Error(`session create failed: ${response.status}`);
  return response.json();
}

export async function renameSession(sessionId: string, title: string): Promise<SessionSummary> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) throw new Error(`session rename failed: ${response.status}`);
  return response.json();
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(`session delete failed: ${response.status}`);
}

export async function listLlmLogs(limit = 100, sessionId?: string): Promise<LlmLogSummary[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (sessionId) params.set("session_id", sessionId);
  const response = await fetch(`${API_BASE}/api/llm-logs?${params.toString()}`);
  if (!response.ok) throw new Error(`llm logs failed: ${response.status}`);
  const payload = await response.json();
  return payload.logs ?? [];
}

export async function getLlmLog(logId: number): Promise<LlmLogDetail> {
  const response = await fetch(`${API_BASE}/api/llm-logs/${logId}`);
  if (!response.ok) throw new Error(`llm log detail failed: ${response.status}`);
  return response.json();
}

export async function clearLlmLogs(): Promise<{ deleted: number }> {
  const response = await fetch(`${API_BASE}/api/llm-logs`, { method: "DELETE" });
  if (!response.ok) throw new Error(`llm log clear failed: ${response.status}`);
  return response.json();
}

export async function listCapabilities(visibility = "external"): Promise<CapabilityModule[]> {
  const response = await fetch(`${API_BASE}/api/capabilities?visibility=${encodeURIComponent(visibility)}`);
  if (!response.ok) throw new Error(`capabilities failed: ${response.status}`);
  const payload = await response.json();
  return payload.capabilities ?? [];
}

export async function updateCapability(
  moduleId: string,
  patch: Partial<Pick<CapabilityModule, "enabled" | "timeout_seconds" | "permissions">>,
): Promise<CapabilityModule> {
  const response = await fetch(`${API_BASE}/api/capabilities/${encodeURIComponent(moduleId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new Error(`capability update failed: ${response.status}`);
  return response.json();
}

export async function checkCapabilityHealth(moduleId: string): Promise<CapabilityModule["health"]> {
  const response = await fetch(`${API_BASE}/api/capabilities/${encodeURIComponent(moduleId)}/health`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(`capability health failed: ${response.status}`);
  return response.json();
}

export async function startResearchThread(input: {
  subject: string;
  subject_type?: string;
  depth?: string;
  user_goal?: string;
  research_goal?: string;
  subject_hint?: string;
  scope_hint?: string;
  budget_profile?: string;
  allowed_tools?: string[];
  blocked_tools?: string[];
  constraints?: string;
  session_id?: string;
  force_new?: boolean;
}): Promise<ResearchThread> {
  const response = await fetch(`${API_BASE}/api/research/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`research thread start failed: ${response.status}`);
  return response.json();
}

export async function listResearchThreads(sessionId?: string, limit = 10, status?: string): Promise<ResearchThread[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (sessionId) params.set("session_id", sessionId);
  if (status) params.set("status", status);
  const response = await fetch(`${API_BASE}/api/research/threads?${params.toString()}`);
  if (!response.ok) throw new Error(`research thread list failed: ${response.status}`);
  const payload = await response.json();
  return payload.threads ?? [];
}

export async function controlResearchThread(threadId: string, action: "pause" | "resume" | "cancel"): Promise<ResearchThread> {
  const response = await fetch(`${API_BASE}/api/research/threads/${encodeURIComponent(threadId)}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!response.ok) throw new Error(`research thread control failed: ${response.status}`);
  const payload = await response.json();
  return payload.thread;
}

export async function listResearchRecords(limit = 20): Promise<ResearchRecordSummary[]> {
  const response = await fetch(`${API_BASE}/api/research/records?limit=${limit}`);
  if (!response.ok) throw new Error(`research records failed: ${response.status}`);
  const payload = await response.json();
  return payload.records ?? [];
}

export async function getResearchRecord(input: {
  recordId: string;
  section?: string;
  offset?: number;
  maxChars?: number;
}): Promise<ResearchRecordDetail> {
  const params = new URLSearchParams();
  if (input.section) params.set("section", input.section);
  if (input.offset != null) params.set("offset", String(input.offset));
  if (input.maxChars != null) params.set("max_chars", String(input.maxChars));
  const encodedRecordId = input.recordId.split("/").map((part) => encodeURIComponent(part)).join("/");
  const response = await fetch(`${API_BASE}/api/research/records/${encodedRecordId}?${params.toString()}`);
  if (!response.ok) throw new Error(`research record detail failed: ${response.status}`);
  return response.json();
}

export async function getDashboardSidebar(): Promise<DashboardSidebarPayload> {
  const response = await fetchWithTimeout(`${API_BASE}/api/dashboard/sidebar`, undefined, DASHBOARD_REQUEST_TIMEOUT_MS);
  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("左侧看板接口未生效，请重启 FinClaw 后端");
    }
    throw new Error(`dashboard sidebar failed: ${response.status}`);
  }
  return response.json();
}

export async function refreshDashboardSidebar(): Promise<DashboardSidebarPayload> {
  const response = await fetchWithTimeout(`${API_BASE}/api/dashboard/sidebar/refresh`, {
    method: "POST",
  }, DASHBOARD_REQUEST_TIMEOUT_MS + 5000);
  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("左侧看板刷新接口未生效，请重启 FinClaw 后端");
    }
    throw new Error(`dashboard sidebar refresh failed: ${response.status}`);
  }
  return response.json();
}

export async function streamRefreshDashboardSidebar(handlers: RefreshStreamHandlers): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/dashboard/sidebar/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    handlers.onError(`刷新请求失败：${error}`);
    throw error;
  }

  if (!response.ok || !response.body) {
    handlers.onError(`刷新请求失败：${response.status} ${response.statusText}`);
    throw new Error(`刷新请求失败：${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      if (part.trim()) {
        parseRefreshSseBlock(part, handlers);
      }
    }
  }
  if (buffer.trim()) {
    parseRefreshSseBlock(buffer, handlers);
  }
}

function parseRefreshSseBlock(block: string, handlers: RefreshStreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const raw of block.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;

  let payload;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch (error) {
    console.error("[parseRefreshSseBlock] JSON parse error:", error);
    return;
  }

  if (event === "sidebar_data") handlers.onSidebarData(payload);
  if (event === "refresh_started") handlers.onRefreshStarted(payload);
  if (event === "refresh_progress") handlers.onRefreshProgress(payload);
  if (event === "refresh_completed") handlers.onRefreshCompleted(payload);
  if (event === "refresh_failed") handlers.onRefreshFailed(payload);
  if (event === "refresh_warning") handlers.onRefreshWarning(payload);
  if (event === "error") handlers.onError(payload.message ?? "未知错误");
}

async function fetchWithTimeout(input: RequestInfo | URL, init?: RequestInit, timeoutMs = DASHBOARD_REQUEST_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(input, {
      ...init,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(`dashboard request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}
