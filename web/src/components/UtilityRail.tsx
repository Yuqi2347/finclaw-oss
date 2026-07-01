import { memo, useEffect, useState } from "react";
import type { AnalysisJob, CapabilityModule, ResearchRecordSummary, ResearchThread, SessionSummary } from "../types";
import { AnalysisJobDock } from "./AnalysisJobDock";
import { MarkdownView } from "./MarkdownView";
import { MemoryDocumentView } from "./MemoryDocumentView";
import { API_BASE, checkCapabilityHealth, controlResearchThread, getResearchRecord, listCapabilities, listResearchRecords, listResearchThreads, updateCapability } from "../api/finclaw";

type Props = {
  session: string;
  sessions: SessionSummary[];
  jobs: AnalysisJob[];
  onJobUpdate: (job: AnalysisJob) => void;
  onJobDismiss: (jobId: string) => void;
  active: boolean;
  status: string;
  sessionError?: string;
};

type MemoryFileStats = {
  exists: boolean;
  entry_count?: number;
  log_count?: number;
  current_level?: string;
  chapter_count?: number;
  dimension_count?: number;
  active_count?: number;
  watching_count?: number;
  last_updated?: string;
  core_updated_at?: string;
  last_candidate_created_at?: string;
  pending_candidate_count?: number;
};

type MemoryStats = {
  profile: MemoryFileStats;
  playbook: MemoryFileStats;
  convictions: MemoryFileStats;
};

type MemoryFile = "profile" | "playbook" | "convictions";

const MEMORY_FILE_LABELS: Record<MemoryFile, string> = {
  profile: "用户画像",
  playbook: "研究框架",
  convictions: "当前投资判断",
};

type MemoryContent = {
  success: boolean;
  content: string;
  metadata?: MemoryFileStats;
};

type MemoryCandidate = {
  candidate_id: string;
  target: MemoryFile;
  content: string;
  reason?: string;
  confidence?: number;
  operation?: string;
  created_at?: string;
};

type UtilityTab = "research" | "records" | "extensions" | "memory" | "jobs";

const EXTERNAL_CAPABILITY_FALLBACK: CapabilityModule[] = [
  {
    id: "tradingagents",
    name: "个股深研",
    visibility: "external",
    category: "扩展研究能力",
    description: "单标的多 Agent 深度研究能力，适合对个股做基本面、技术面、新闻舆情、风险与交易视角的多维研究。",
    best_for: ["个股深度研究", "公司基本面", "交易视角"],
    skill: "background-research-engines",
    tools: ["run_stock_research"],
    permissions: ["network", "market_data", "long_running", "expensive"],
    available_permissions: ["network", "market_data", "long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
    health: { status: "unknown", message: "等待后端能力 API 生效" },
  },
  {
    id: "bettafish",
    name: "主线雷达",
    visibility: "external",
    category: "扩展研究能力",
    description: "市场主线与题材研究引擎，适合梳理结构性行情线索。",
    best_for: ["市场主线", "题材扩散", "产业链线索"],
    skill: "background-research-engines",
    tools: ["run_market_discovery"],
    permissions: ["network", "market_data", "long_running", "expensive"],
    available_permissions: ["network", "market_data", "long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
    health: { status: "unknown", message: "等待后端能力 API 生效" },
  },
  {
    id: "tradinggraph",
    name: "产业链透视",
    visibility: "external",
    category: "扩展研究能力",
    description: "产业链图谱与瓶颈分析能力，适合图谱化推理和结构性机会分析。",
    best_for: ["产业链图谱", "瓶颈节点", "主线结构"],
    skill: "tradinggraph",
    tools: ["control_industry_graph", "read_industry_graph", "read_industry_graph_node"],
    permissions: ["network", "market_data", "long_running", "expensive"],
    available_permissions: ["network", "market_data", "long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
    health: { status: "unknown", message: "等待后端能力 API 生效" },
  },
];

function ResearchRounds({ thread, formatDuration }: { thread: ResearchThread; formatDuration: (ms?: unknown) => string }) {
  const rounds = thread.rounds ?? [];
  if (!rounds.length) {
    return <div className="research-muted">线程已启动，正在等待第一轮研究动作。</div>;
  }
  return (
    <div className="research-round-list">
      {rounds.map((round) => (
        <div key={round.round} className="research-round">
          <div className="research-round-head">
            <span className={`research-dot ${round.status || "pending"}`} />
            <strong>第 {round.round} 轮</strong>
            <em>{round.validator_status ? `审稿 ${round.validator_status}` : (round.status || "pending")}</em>
          </div>
          {round.focus && <div className="research-round-focus">{round.focus}</div>}
          {!!round.tools?.length && (
            <div className="research-round-tools">
              {round.tools.slice(0, 6).map((tool, index) => (
                <div key={`${round.round}-${tool.tool}-${index}`} className="research-round-tool">
                  <span>{tool.tool || "tool"}</span>
                  <em>{tool.status || "unknown"} · {formatDuration(tool.elapsed_ms)}</em>
                </div>
              ))}
            </div>
          )}
          {round.summary && <div className="research-round-summary">{round.summary}</div>}
        </div>
      ))}
    </div>
  );
}

export const UtilityRail = memo(function UtilityRail({ session, sessions, jobs, onJobUpdate, onJobDismiss, active, status, sessionError }: Props) {
  const running = jobs.filter((job) => job.status === "running" || job.status === "cancelling").length;
  const failed = jobs.filter((job) => job.status === "failed").length;

  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [memoryError, setMemoryError] = useState("");
  const [expanded, setExpanded] = useState<MemoryFile | null>(null);
  const [content, setContent] = useState<Record<MemoryFile, string>>({ profile: "", playbook: "", convictions: "" });
  const [contentError, setContentError] = useState<Record<MemoryFile, string>>({ profile: "", playbook: "", convictions: "" });
  const [loadingContent, setLoadingContent] = useState<MemoryFile | null>(null);
  const [editing, setEditing] = useState<MemoryFile | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [candidateBusy, setCandidateBusy] = useState<string | null>(null);
  const [researchThreads, setResearchThreads] = useState<ResearchThread[]>([]);
  const [researchRecords, setResearchRecords] = useState<ResearchRecordSummary[]>([]);
  const [researchError, setResearchError] = useState("");
  const [capabilities, setCapabilities] = useState<CapabilityModule[]>([]);
  const [capabilityError, setCapabilityError] = useState("");
  const [capabilityBusy, setCapabilityBusy] = useState<string | null>(null);
  const [capabilityReadonly, setCapabilityReadonly] = useState(false);
  const [expandedCapability, setExpandedCapability] = useState<string | null>(null);
  const [expandedThread, setExpandedThread] = useState<string | null>(null);
  const [expandedRecord, setExpandedRecord] = useState<string | null>(null);
  const [recordView, setRecordView] = useState<Record<string, "summary" | "body">>({});
  const [recordWindows, setRecordWindows] = useState<Record<string, { content: string; nextOffset?: number | null; hasMore?: boolean; totalChars?: number }>>({});
  const [recordLoading, setRecordLoading] = useState<string | null>(null);
  const [openTab, setOpenTab] = useState<UtilityTab | null>(null);

  async function loadMemoryContent(file: MemoryFile) {
    setLoadingContent(file);
    setContentError((prev) => ({ ...prev, [file]: "" }));
    try {
      const r = await fetch(`${API_BASE}/api/memory/${file}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data: MemoryContent = await r.json();
      if (!data.success) throw new Error("memory content failed");
      setContent((prev) => ({ ...prev, [file]: data.content ?? "" }));
      if (data.metadata) {
        setStats((prev) => ({
          ...(prev ?? {
            profile: { exists: false },
            playbook: { exists: false },
            convictions: { exists: false },
          }),
          [file]: { exists: true, ...data.metadata },
        }));
      }
    } catch (error) {
      setContentError((prev) => ({
        ...prev,
        [file]: error instanceof Error ? error.message : "加载失败",
      }));
    } finally {
      setLoadingContent((current) => (current === file ? null : current));
    }
  }

  async function loadMemoryStats() {
    setMemoryError("");
    try {
      const r = await fetch(`${API_BASE}/api/memory/stats`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.success) throw new Error(data.detail || "memory stats failed");
      setStats(data.stats);
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "长期记忆加载失败");
    }
  }

  async function loadMemoryCandidates() {
    try {
      const r = await fetch(`${API_BASE}/api/memory/candidates?status=pending`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.success) throw new Error(data.detail || "memory candidates failed");
      setCandidates(data.candidates ?? []);
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "候选记忆加载失败");
    }
  }

  useEffect(() => {
    void loadMemoryStats();
    void loadMemoryCandidates();
    void loadResearchAssets();
    void loadCapabilities();
  }, []);

  useEffect(() => {
    function handleMemoryCandidateCreated() {
      void loadMemoryCandidates();
      void loadMemoryStats();
    }
    window.addEventListener("finclaw:memory-candidate-created", handleMemoryCandidateCreated);
    window.addEventListener("finclaw:research-thread-created", handleResearchChanged);
    return () => {
      window.removeEventListener("finclaw:memory-candidate-created", handleMemoryCandidateCreated);
      window.removeEventListener("finclaw:research-thread-created", handleResearchChanged);
    };
  }, [session]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadResearchAssets(true);
    }, 4000);
    return () => window.clearInterval(interval);
  }, [session]);

  function handleResearchChanged() {
    void loadResearchAssets();
  }

  async function loadResearchAssets(silent = false) {
    if (!silent) setResearchError("");
    try {
      const [threads, records] = await Promise.all([
        listResearchThreads(session, 8, "active"),
        listResearchRecords(8),
      ]);
      setResearchThreads(threads);
      setResearchRecords(records);
    } catch (error) {
      if (!silent) setResearchError(error instanceof Error ? error.message : "研究资产加载失败");
    }
  }

  async function loadCapabilities() {
    setCapabilityError("");
    try {
      const modules = await listCapabilities("external");
      setCapabilities(modules);
      void hydrateCapabilityHealth(modules);
      setCapabilityReadonly(false);
    } catch (error) {
      setCapabilities(EXTERNAL_CAPABILITY_FALLBACK);
      setCapabilityReadonly(true);
      const message = error instanceof Error ? error.message : "扩展能力加载失败";
      setCapabilityError(`${message}。当前显示本地只读清单，请重启 FinClaw 后端加载扩展能力 API。`);
    }
  }

  async function hydrateCapabilityHealth(modules: CapabilityModule[]) {
    const enabledModules = modules.filter((module) => module.enabled);
    if (!enabledModules.length) return;
    const results = await Promise.all(enabledModules.map(async (module) => {
      try {
        return { id: module.id, health: await checkCapabilityHealth(module.id) };
      } catch (error) {
        return {
          id: module.id,
          health: {
            status: "error",
            message: error instanceof Error ? error.message : "健康检查失败",
          },
        };
      }
    }));
    setCapabilities((prev) => prev.map((module) => {
      const result = results.find((item) => item.id === module.id);
      return result ? { ...module, health: result.health } : module;
    }));
  }

  async function saveCapability(module: CapabilityModule, patch: Partial<Pick<CapabilityModule, "enabled" | "timeout_seconds" | "permissions">>) {
    if (capabilityReadonly) {
      setCapabilityError("当前扩展能力接口未生效，配置无法保存；请重启 FinClaw 后端后再修改。");
      return;
    }
    setCapabilityBusy(module.id);
    setCapabilityError("");
    try {
      const updated = await updateCapability(module.id, patch);
      setCapabilities((prev) => prev.map((item) => item.id === updated.id ? updated : item));
      if (updated.enabled && "enabled" in patch) {
        void hydrateCapabilityHealth([updated]);
      }
    } catch (error) {
      setCapabilityError(error instanceof Error ? error.message : "扩展能力保存失败");
    } finally {
      setCapabilityBusy((current) => current === module.id ? null : current);
    }
  }

  async function refreshCapabilityHealth(module: CapabilityModule) {
    if (capabilityReadonly) {
      setCapabilityError("当前扩展能力接口未生效，无法执行健康检查；请重启 FinClaw 后端后再试。");
      return;
    }
    setCapabilityBusy(module.id);
    setCapabilityError("");
    try {
      const health = await checkCapabilityHealth(module.id);
      setCapabilities((prev) => prev.map((item) => item.id === module.id ? { ...item, health } : item));
    } catch (error) {
      setCapabilityError(error instanceof Error ? error.message : "健康检查失败");
    } finally {
      setCapabilityBusy((current) => current === module.id ? null : current);
    }
  }

  async function controlThread(thread: ResearchThread, action: "pause" | "resume" | "cancel") {
    setResearchError("");
    try {
      const updated = await controlResearchThread(thread.thread_id, action);
      setResearchThreads((prev) => prev.map((item) => item.thread_id === updated.thread_id ? updated : item));
      window.setTimeout(() => void loadResearchAssets(true), 800);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : "研究线程控制失败");
    }
  }

  async function readRecord(record: ResearchRecordSummary, section: "summary" | "body" = "summary", append = false) {
    const key = `${record.record_id}::${section || ""}`;
    const current = recordWindows[key];
    setRecordLoading(key);
    setResearchError("");
    try {
      const detail = await getResearchRecord({
        recordId: record.record_id,
        section,
        offset: append ? current?.nextOffset ?? 0 : 0,
        maxChars: 5000,
      });
      setRecordWindows((prev) => ({
        ...prev,
        [key]: {
          content: append ? `${current?.content || ""}${detail.read_window.content}` : detail.read_window.content,
          nextOffset: detail.read_window.next_offset,
          hasMore: detail.read_window.has_more,
          totalChars: detail.read_window.total_chars,
        },
      }));
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : "研究档案读取失败");
    } finally {
      setRecordLoading((currentKey) => (currentKey === key ? null : currentKey));
    }
  }

  async function toggleExpand(file: MemoryFile) {
    if (expanded === file) {
      setExpanded(null);
      return;
    }
    setExpanded(file);
    void loadMemoryContent(file);
  }

  function startEdit(file: MemoryFile) {
    if (file === "profile") return;
    setEditing(file);
    setEditDraft(content[file]);
  }

  async function saveEdit(file: MemoryFile) {
    if (file === "profile") {
      setMemoryError("用户画像由 Agent 自动维护，不支持手动编辑");
      setEditing(null);
      return;
    }
    setSaving(true);
    try {
      const r = await fetch(`${API_BASE}/api/memory/${file}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editDraft }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.success) throw new Error(data.detail || "保存失败");
      setContent((prev) => ({ ...prev, [file]: editDraft }));
      setEditing(null);
      await loadMemoryStats();
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function decideCandidate(candidate: MemoryCandidate, approved: boolean) {
    setCandidateBusy(candidate.candidate_id);
    try {
      const r = await fetch(`${API_BASE}/api/memory/candidates/${candidate.candidate_id}/${approved ? "approve" : "reject"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: approved ? undefined : JSON.stringify({ reason: "用户在右栏拒绝候选记忆" }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.success) throw new Error(data.detail || "候选处理失败");
      setCandidates((prev) => prev.filter((item) => item.candidate_id !== candidate.candidate_id));
      if (approved) {
        setContent((prev) => ({ ...prev, [candidate.target]: "" }));
        await loadMemoryStats();
      }
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "候选处理失败");
    } finally {
      setCandidateBusy(null);
    }
  }

  function formatMemoryTime(value?: string | null) {
    if (!value || value === "未知") return "未知";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function memoryHint(file: MemoryFile, fileStats: MemoryFileStats) {
    if (!fileStats.exists) return "暂无记录";
    const count =
      file === "profile"
        ? `${fileStats.current_level ?? "Level ?"} · LOG ${fileStats.log_count ?? 0}/8`
        : file === "playbook"
          ? `${fileStats.dimension_count ?? fileStats.chapter_count ?? 0} 维度`
          : `${fileStats.active_count ?? 0} active / ${fileStats.watching_count ?? 0} watching`;
    const coreTime = formatMemoryTime(fileStats.core_updated_at || fileStats.last_updated);
    const pending = fileStats.pending_candidate_count ? ` · ${fileStats.pending_candidate_count} 待确认` : "";
    const candidate = fileStats.last_candidate_created_at ? ` · 候选 ${formatMemoryTime(fileStats.last_candidate_created_at)}` : "";
    return `${count} · 核心 ${coreTime}${pending}${candidate}`;
  }

  const CARDS: { file: MemoryFile; label: string; hint: (s: MemoryStats) => string }[] = [
    {
      file: "profile",
      label: MEMORY_FILE_LABELS.profile,
      hint: (s) => memoryHint("profile", s.profile),
    },
    {
      file: "playbook",
      label: MEMORY_FILE_LABELS.playbook,
      hint: (s) => memoryHint("playbook", s.playbook),
    },
    {
      file: "convictions",
      label: MEMORY_FILE_LABELS.convictions,
      hint: (s) => memoryHint("convictions", s.convictions),
    },
  ];

  function formatDuration(ms?: unknown) {
    const value = typeof ms === "number" ? ms : Number(ms ?? 0);
    if (!Number.isFinite(value) || value <= 0) return "耗时 -";
    if (value < 1000) return `${Math.round(value)}ms`;
    if (value < 60_000) return `${(value / 1000).toFixed(1)}s`;
    return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1000)}s`;
  }

  function capabilityPermissionLabel(permission: string) {
    if (permission === "network") return "联网";
    if (permission === "market_data") return "行情数据";
    if (permission === "long_running") return "长任务";
    if (permission === "expensive") return "高成本";
    return permission;
  }

  function memoryCandidateActionLabel(candidate: MemoryCandidate) {
    const operation = String(candidate.operation || "ADD").toUpperCase();
    if (operation === "UPDATE") return "更新";
    if (operation === "WEAKEN") return "降级观察";
    if (operation === "ARCHIVE" || operation === "CONFLICT") return "归档";
    return "写入";
  }

  function capabilityStatusTone(module: CapabilityModule) {
    if (!module.enabled) return "disabled";
    const status = String(module.health?.status || "unknown").toLowerCase();
    if (["ok", "healthy", "enabled", "ready", "online"].includes(status)) return "ok";
    if (["error", "failed", "unhealthy", "offline"].includes(status)) return "error";
    if (status === "warning") return "warning";
    return "unknown";
  }

  function capabilityStatusLabel(module: CapabilityModule) {
    const tone = capabilityStatusTone(module);
    if (tone === "disabled") return "已停用";
    if (tone === "ok") return "已启用，健康正常";
    if (tone === "error") return "已启用，健康异常";
    if (tone === "warning") return "已启用，存在风险";
    return "已启用，健康未知";
  }

  const navItems: Array<{ tab: UtilityTab; label: string; title: string; mark: string; count: number; tone?: "attention" | "danger" }> = [
    { tab: "research", label: "研究", title: "深度研究", mark: "DR", count: researchThreads.length, tone: researchThreads.length ? "attention" : undefined },
    { tab: "records", label: "档案", title: "深度研究档案", mark: "AR", count: researchRecords.length },
    { tab: "extensions", label: "扩展", title: "扩展能力", mark: "EX", count: capabilities.filter((item) => item.enabled).length },
    { tab: "memory", label: "记忆", title: "长期记忆", mark: "M", count: candidates.length, tone: candidates.length ? "attention" : undefined },
    { tab: "jobs", label: "任务", title: "后台任务", mark: "J", count: running + failed, tone: failed ? "danger" : running ? "attention" : undefined },
  ];

  function toggleTab(tab: UtilityTab) {
    setOpenTab((current) => current === tab ? null : tab);
  }

  return (
    <aside className={`utility-dock ${openTab ? "open" : ""}`}>
      <nav className="utility-dock-nav" aria-label="辅助面板">
        {navItems.map((item) => (
          <button
            key={item.tab}
            type="button"
            className={`utility-dock-tab ${openTab === item.tab ? "active" : ""} ${item.tone ?? ""}`}
            onClick={() => toggleTab(item.tab)}
            aria-pressed={openTab === item.tab}
            aria-label={item.title}
          >
            <i className="utility-dock-mark" aria-hidden="true">{item.mark}</i>
            <span>{item.label}</span>
            {item.count ? <em>{item.count}</em> : null}
          </button>
        ))}
      </nav>

      {openTab && (
        <div className="utility-drawer" role="complementary">
          <div className="utility-drawer-head">
            <div>
              <span className="utility-drawer-kicker">Workspace</span>
              <h2>{navItems.find((item) => item.tab === openTab)?.title}</h2>
            </div>
            <button type="button" onClick={() => setOpenTab(null)} aria-label="关闭辅助面板">×</button>
          </div>

          <div className="utility-rail">
            {openTab === "research" && (
              <section className="utility-card">
                {researchError && (
                  <div className="rail-error rail-error-block">{researchError}</div>
                )}
                <div className="research-rail-list">
                  {researchThreads.length === 0 ? (
                    <div className="rail-empty">
                      在输入框填写研究对象后点击“开启研究”，这里会显示后台进度。
                    </div>
                  ) : researchThreads.map((thread) => (
                    <div key={thread.thread_id} className="research-thread-card">
                      <button type="button" className="research-card-main" onClick={() => setExpandedThread(expandedThread === thread.thread_id ? null : thread.thread_id)}>
                        <div>
                          <div className="research-card-title">{thread.subject}</div>
                          <div className="research-card-sub">{thread.depth} · {thread.rounds?.length ?? 0} 轮 · {formatDuration((thread.metrics as { wall_elapsed_ms?: number } | undefined)?.wall_elapsed_ms)}</div>
                        </div>
                        <span className={`research-status ${thread.status}`}>{thread.status}</span>
                      </button>
                      {expandedThread === thread.thread_id && (
                        <div className="research-detail">
                          <div className="research-badges">
                            <span>当前 {thread.rounds?.length ? `第 ${thread.rounds[thread.rounds.length - 1].round} 轮` : "准备中"}</span>
                            <span>工具 {(thread.metrics as { tool_calls_used?: number } | undefined)?.tool_calls_used ?? 0}</span>
                          </div>
                          <p className="research-card-conclusion">
                            {thread.current_conclusion || "等待研究进度"}
                          </p>
                          <div className="research-detail-block">
                            <div className="research-detail-title">轮次进度</div>
                            <ResearchRounds thread={thread} formatDuration={formatDuration} />
                          </div>
                        </div>
                      )}
                      <div className="research-controls">
                        {thread.status === "in_progress" && (
                          <button type="button" className="mini-rail-btn" onClick={() => void controlThread(thread, "pause")}>暂停</button>
                        )}
                        {["paused", "failed"].includes(thread.status) && (
                          <button type="button" className="mini-rail-btn" onClick={() => void controlThread(thread, "resume")}>恢复</button>
                        )}
                        {!["completed", "cancelled"].includes(thread.status) && (
                          <button type="button" className="mini-rail-btn danger" onClick={() => void controlThread(thread, "cancel")}>取消</button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {openTab === "records" && (
              <section className="utility-card">
                <div className="research-rail-list">
                  {researchRecords.length === 0 ? (
                    <div className="rail-empty">完成研究线程后会自动沉淀。</div>
                  ) : researchRecords.map((record) => (
                    <div key={record.record_id} className="research-record-card">
                      <button
                        type="button"
                        className="research-card-main"
                        onClick={() => {
                          const next = expandedRecord === record.record_id ? null : record.record_id;
                          setExpandedRecord(next);
                          if (next) {
                            setRecordView((prev) => ({ ...prev, [record.record_id]: prev[record.record_id] || "summary" }));
                            void readRecord(record, "summary");
                          }
                        }}
                      >
                        <div>
                          <div className="research-card-title">{record.title}</div>
                          <div className="research-card-sub">
                            {record.updated_at || "未更新"} · {record.quality_level || "研究档案"}
                          </div>
                        </div>
                        <span className="research-record-gap">报告</span>
                      </button>
                      {expandedRecord === record.record_id && (
                        <div className="research-detail">
                          <p className="research-card-conclusion">
                            {record.user_goal ? `目标：${record.user_goal}` : (record.core_conclusion || record.record_id)}
                          </p>
                          <div className="research-section-strip">
                            {(["summary", "body"] as const).map((view) => (
                              <button
                                key={view}
                                type="button"
                                className={(recordView[record.record_id] || "summary") === view ? "active" : ""}
                                onClick={() => {
                                  setRecordView((prev) => ({ ...prev, [record.record_id]: view }));
                                  void readRecord(record, view);
                                }}
                              >
                                {view === "summary" ? "摘要" : "正文"}
                              </button>
                            ))}
                          </div>
                          {(() => {
                            const selected = recordView[record.record_id] || "summary";
                            const key = `${record.record_id}::${selected}`;
                            const window = recordWindows[key];
                            return (
                              <div className="research-record-reader">
                                {recordLoading === key && !window ? (
                                  <div className="research-muted">读取中...</div>
                                ) : (
                                  <MarkdownView content={window?.content || "暂无内容"} variant="panel" />
                                )}
                                {window?.hasMore && (
                                  <button type="button" className="mini-rail-btn" disabled={recordLoading === key} onClick={() => void readRecord(record, selected, true)}>
                                    {recordLoading === key ? "读取中..." : "继续读取"}
                                  </button>
                                )}
                              </div>
                            );
                          })()}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {openTab === "extensions" && (
              <section className="utility-card capability-panel">
                {capabilityError && <div className="rail-error rail-error-block">{capabilityError}</div>}
                <div className="capability-overview">
                  <div>
                    <strong>{capabilities.filter((item) => item.enabled).length}</strong>
                    <span>{capabilityReadonly ? "只读清单" : "已启用"}</span>
                  </div>
                  <button type="button" className="mini-rail-btn" onClick={() => void loadCapabilities()}>刷新</button>
                </div>
                <div className="capability-list">
                  {capabilities.length === 0 ? (
                    <div className="rail-empty">暂无外部扩展能力。</div>
                  ) : capabilities.map((module) => {
                    const expanded = expandedCapability === module.id;
                    const busy = capabilityBusy === module.id;
                    const statusTone = capabilityStatusTone(module);
                    const statusLabel = capabilityStatusLabel(module);
                    return (
                      <div key={module.id} className={`capability-card ${module.enabled ? "enabled" : "disabled"}`}>
                        <button type="button" className="capability-card-main" onClick={() => setExpandedCapability(expanded ? null : module.id)}>
                          <div>
                            <div className="capability-card-title">
                              <span className={`capability-health-dot ${statusTone}`} title={statusLabel} aria-label={statusLabel} />
                              {module.name}
                            </div>
                            <div className="capability-card-sub">{module.description}</div>
                          </div>
                          <span className={`capability-state ${statusTone}`}>{module.enabled ? "启用" : "停用"}</span>
                        </button>
                        <div className="capability-tags">
                          {module.best_for.slice(0, 3).map((item) => <span key={item}>{item}</span>)}
                        </div>
                        {expanded && (
                          <div className="capability-detail">
                            <div className="capability-setting-row">
                              <span>模块状态</span>
                              <button
                                type="button"
                                className={`capability-switch ${module.enabled ? "on" : "off"}`}
                                disabled={busy || capabilityReadonly}
                                onClick={() => void saveCapability(module, { enabled: !module.enabled })}
                              >
                                <i />
                              </button>
                            </div>
                            <label className="capability-setting-row">
                              <span>超时时间</span>
                              <input
                                type="number"
                                min={60}
                                max={7200}
                                step={60}
                                defaultValue={module.timeout_seconds}
                                disabled={capabilityReadonly}
                                onBlur={(event) => {
                                  const value = Number(event.currentTarget.value);
                                  if (Number.isFinite(value) && value !== module.timeout_seconds) {
                                    void saveCapability(module, { timeout_seconds: value });
                                  }
                                }}
                              />
                            </label>
                            <div className="capability-permissions">
                              {module.available_permissions.map((permission) => {
                                const activePermission = module.permissions.includes(permission);
                                return (
                                  <button
                                    key={permission}
                                    type="button"
                                    className={activePermission ? "active" : ""}
                                    disabled={busy || capabilityReadonly}
                                    onClick={() => {
                                      const next = activePermission
                                        ? module.permissions.filter((item) => item !== permission)
                                        : [...module.permissions, permission];
                                      void saveCapability(module, { permissions: next });
                                    }}
                                  >
                                    {capabilityPermissionLabel(permission)}
                                  </button>
                                );
                              })}
                            </div>
                            <div className="capability-actions">
                              <button type="button" className="mini-rail-btn" disabled={busy || capabilityReadonly} onClick={() => void refreshCapabilityHealth(module)}>
                                {busy ? "检查中" : "健康检查"}
                              </button>
                              <span className="capability-health-message" title={module.health?.message || "状态未知"}>
                                {module.health?.message || "状态未知"}
                              </span>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            )}

            {openTab === "memory" && (
              <section className="utility-card utility-card-memory">
                {memoryError && (
                  <div className="rail-alert">
                    <span>{memoryError}</span>
                    <button type="button" onClick={() => void loadMemoryStats()}>重试</button>
                  </div>
                )}

                {CARDS.map(({ file, label, hint }) => (
                  <div key={file} className="memory-card">
                    <button
                      type="button"
                      onClick={() => void toggleExpand(file)}
                      className={`memory-card-toggle ${expanded === file ? "expanded" : ""}`}
                    >
                      <div>
                        <div className="memory-card-title">{label}</div>
                        <div className="memory-card-hint">{stats ? hint(stats) : "—"}</div>
                      </div>
                      <span className="memory-card-chevron">{expanded === file ? "▾" : "▸"}</span>
                    </button>

                    {expanded === file && (
                      <div className="memory-card-body">
                        {editing === file ? (
                          <>
                            <textarea value={editDraft} onChange={(e) => setEditDraft(e.target.value)} className="rail-textarea" />
                            <div className="rail-actions">
                              <button type="button" disabled={saving} onClick={() => void saveEdit(file)} className="rail-btn primary">
                                {saving ? "保存中…" : "保存"}
                              </button>
                              <button type="button" onClick={() => setEditing(null)} className="rail-btn">取消</button>
                            </div>
                          </>
                        ) : (
                          <>
                            <div className="memory-card-toolbar">
                              {file === "profile" ? (
                                <span>只读 · Agent 自动维护</span>
                              ) : (
                                <button type="button" onClick={() => startEdit(file)}>编辑</button>
                              )}
                            </div>
                            {loadingContent === file ? (
                              <div className="rail-muted">加载中…</div>
                            ) : contentError[file] ? (
                              <div className="rail-error">加载失败：{contentError[file]}</div>
                            ) : (
                              <div className="memory-content">
                                <MemoryDocumentView type={file} content={content[file] || ""} />
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                ))}

                <div className="candidate-memory">
                  <div className="candidate-memory-head">
                    <div>候选记忆</div>
                    <button type="button" onClick={() => void loadMemoryCandidates()}>刷新 · {candidates.length}</button>
                  </div>

                  {candidates.length === 0 ? (
                    <div className="candidate-empty">暂无待确认候选</div>
                  ) : candidates.map((candidate) => (
                    <div key={candidate.candidate_id} className="candidate-card">
                      <div className="candidate-card-head">
                        <div>{memoryCandidateActionLabel(candidate)} {MEMORY_FILE_LABELS[candidate.target] ?? candidate.target}</div>
                        <div>{candidate.confidence ? `置信度 ${Math.round(candidate.confidence * 100)}%` : candidate.created_at || ""}</div>
                      </div>
                      {candidate.reason && <div className="candidate-reason">{candidate.reason}</div>}
                      <div className="candidate-content">
                        <MemoryDocumentView type={candidate.target} content={candidate.content} compact />
                      </div>
                      <div className="rail-actions">
                        <button type="button" disabled={candidateBusy === candidate.candidate_id} onClick={() => void decideCandidate(candidate, true)} className="rail-btn primary">
                          确认写入
                        </button>
                        <button type="button" disabled={candidateBusy === candidate.candidate_id} onClick={() => void decideCandidate(candidate, false)} className="rail-btn danger">
                          拒绝
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {openTab === "jobs" && (
              <section className="utility-card utility-card-jobs">
                <AnalysisJobDock jobs={jobs} onUpdate={onJobUpdate} onDismiss={onJobDismiss} inline />
              </section>
            )}
          </div>
        </div>
      )}
    </aside>
  );
});
