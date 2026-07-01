import type { CapabilityModule, PendingAction } from "../types";
import { listCapabilities } from "../api/finclaw";
import { useEffect, useState } from "react";

type Props = {
  action: PendingAction;
  onConfirm: (actionId: string, approved: boolean, argumentsOverride?: Record<string, unknown>) => Promise<void>;
};

export function PendingActionCard({ action, onConfirm }: Props) {
  const [state, setState] = useState<"pending" | "running" | "confirmed" | "cancelled" | "error">("pending");
  const [error, setError] = useState("");
  const [editableArgs, setEditableArgs] = useState<Record<string, string>>(() => initialEditableArgs(action.arguments));
  const [capabilities, setCapabilities] = useState<CapabilityModule[]>([]);
  const isTransactionAction = action.tool_name === "record_portfolio_transaction";
  const isResearchAction = action.tool_name === "start_research_thread";
  const mergedArguments = isTransactionAction
    ? buildArguments(action.arguments, editableArgs)
    : isResearchAction
      ? buildResearchArguments(action.arguments, editableArgs, capabilities)
      : action.arguments;
  const missingTransactionFields = isTransactionAction ? getMissingTransactionFields(mergedArguments) : [];
  const missingResearchFields = isResearchAction ? getMissingResearchFields(mergedArguments) : [];
  const canApprove = (!isTransactionAction || missingTransactionFields.length === 0) && (!isResearchAction || missingResearchFields.length === 0);

  useEffect(() => {
    setState("pending");
    setError("");
    setEditableArgs(initialEditableArgs(action.arguments, capabilities));
  }, [action.action_id]);

  useEffect(() => {
    if (!isResearchAction || !capabilities.length) return;
    setEditableArgs((prev) => mergeCapabilitySwitches(action.arguments, capabilities, prev));
  }, [action.action_id, isResearchAction, capabilities]);

  useEffect(() => {
    if (!isResearchAction) return;
    let cancelled = false;
    listCapabilities("external")
      .then((items) => {
        if (!cancelled) setCapabilities(items);
      })
      .catch(() => {
        if (!cancelled) setCapabilities(EXTERNAL_CAPABILITY_FALLBACK);
      });
    return () => {
      cancelled = true;
    };
  }, [isResearchAction]);

  async function handleConfirm(approved: boolean) {
    if (approved && !canApprove) {
      const missing = isResearchAction ? missingResearchFields.map(researchFieldLabel) : missingTransactionFields.map(fieldLabel);
      setError(`请先补全 ${missing.join("、")}。`);
      setState("pending");
      return;
    }
    setState("running");
    setError("");
    try {
      await onConfirm(action.action_id, approved, approved && (isTransactionAction || isResearchAction) ? mergedArguments : undefined);
      setState(approved ? "confirmed" : "cancelled");
    } catch (err) {
      setError(String(err));
      setState("error");
    }
  }

  return (
    <div className="pending-action">
      <div className="pending-title">需要确认：{action.tool_name}</div>
      {isTransactionAction ? (
        <TransactionEditor
          args={action.arguments}
          editableArgs={editableArgs}
          missingFields={missingTransactionFields}
          onChange={(key, value) => setEditableArgs((prev) => ({ ...prev, [key]: value }))}
        />
      ) : isResearchAction ? (
        <ResearchEditor
          args={action.arguments}
          editableArgs={editableArgs}
          capabilities={capabilities.length ? capabilities : EXTERNAL_CAPABILITY_FALLBACK}
          missingFields={missingResearchFields}
          onChange={(key, value) => setEditableArgs((prev) => ({ ...prev, [key]: value }))}
        />
      ) : (
        <pre>{JSON.stringify(action.arguments, null, 2)}</pre>
      )}
      {state === "pending" ? (
        <div className="pending-buttons">
          <button type="button" className="primary" disabled={!canApprove} onClick={() => handleConfirm(true)}>确认执行</button>
          <button type="button" onClick={() => handleConfirm(false)}>取消</button>
        </div>
      ) : state === "running" ? (
        <div className="pending-state confirmed">正在提交确认...</div>
      ) : state === "error" ? (
        <>
          <div className="pending-state cancelled">确认失败：{error || "请稍后重试"}</div>
          <div className="pending-buttons">
            <button type="button" className="primary" disabled={!canApprove} onClick={() => handleConfirm(true)}>重新提交</button>
            <button type="button" onClick={() => setState("pending")}>继续修改</button>
          </div>
        </>
      ) : (
        <div className={`pending-state ${state}`}>
          {state === "confirmed" ? "已确认，正在执行" : "已取消"}
        </div>
      )}
    </div>
  );
}

function ResearchEditor({
  args,
  editableArgs,
  capabilities,
  missingFields,
  onChange,
}: {
  args: Record<string, unknown>;
  editableArgs: Record<string, string>;
  capabilities: CapabilityModule[];
  missingFields: string[];
  onChange: (key: string, value: string) => void;
}) {
  const toolPolicy = researchToolPolicy(editableArgs, capabilities);
  return (
    <div className="research-editor">
      <div className="research-editor-summary">
        <strong>{editableArgs.research_goal || editableArgs.user_goal || stringValue(args.research_goal) || stringValue(args.user_goal) || "未命名研究"}</strong>
        <span>{editableArgs.subject || stringValue(args.subject) || "对象待确认"} · {researchSubjectTypeLabel(editableArgs.subject_type || stringValue(args.subject_type))} · {researchDepthLabel(editableArgs.budget_profile || editableArgs.depth || stringValue(args.budget_profile) || stringValue(args.depth))}</span>
      </div>
      {missingFields.length ? (
        <div className="transaction-editor-alert">请补全 {missingFields.map(researchFieldLabel).join("、")} 后确认。</div>
      ) : (
        <div className="transaction-editor-hint">确认后将创建后台 Deep Research Agent；外部扩展能力只会在下方授权范围内使用。</div>
      )}
      <div className="research-editor-grid">
        <label className="transaction-editor-field research-editor-wide">
          <span>研究目标</span>
          <textarea value={editableArgs.research_goal ?? ""} onChange={(event) => onChange("research_goal", event.target.value)} rows={3} />
        </label>
        <label className="transaction-editor-field">
          <span>研究对象</span>
          <input value={editableArgs.subject ?? ""} onChange={(event) => onChange("subject", event.target.value)} />
        </label>
        <label className="transaction-editor-field">
          <span>对象类型</span>
          <select value={editableArgs.subject_type ?? "unknown"} onChange={(event) => onChange("subject_type", event.target.value)}>
            <option value="stock">个股</option>
            <option value="mainline">主线/产业链</option>
            <option value="market">市场</option>
            <option value="comparison">对比</option>
            <option value="unknown">不确定</option>
          </select>
        </label>
        <label className="transaction-editor-field">
          <span>预算档位</span>
          <select value={editableArgs.budget_profile ?? editableArgs.depth ?? "standard"} onChange={(event) => onChange("budget_profile", event.target.value)}>
            <option value="quick">快速</option>
            <option value="standard">标准</option>
            <option value="deep">深度</option>
          </select>
        </label>
        <label className="transaction-editor-field research-editor-wide">
          <span>范围与约束</span>
          <textarea value={editableArgs.constraints ?? ""} onChange={(event) => onChange("constraints", event.target.value)} rows={2} placeholder="例如：不使用产业链透视；只验证近期公告；重点看A股映射" />
        </label>
      </div>
      <div className="research-tool-policy">
        <div className="research-tool-policy-head">
          <span>外部扩展能力</span>
          <small>关闭后本线程不会调用该外部模块；内置数据、联网和记忆能力由系统自动管理</small>
        </div>
        <div className="research-tool-grid">
          {capabilities.map((module) => {
            const enabled = module.enabled && editableArgs[`module:${module.id}`] !== "off" && module.tools.some((tool) => toolPolicy.allowed.has(tool));
            return (
              <button
                key={module.id}
                type="button"
                className={`research-tool-chip ${enabled ? "enabled" : "disabled"} ${module.permissions.includes("expensive") ? "expensive" : ""}`}
                disabled={!module.enabled}
                onClick={() => onChange(`module:${module.id}`, enabled ? "off" : "on")}
              >
                <span>{module.name}</span>
                <small>{module.enabled ? (module.permissions.includes("long_running") ? "长任务" : "扩展") : "全局停用"}</small>
              </button>
            );
          })}
        </div>
      </div>
      <details className="transaction-editor-raw">
        <summary>查看原始参数</summary>
        <pre>{JSON.stringify(args, null, 2)}</pre>
      </details>
    </div>
  );
}

function TransactionEditor({
  args,
  editableArgs,
  missingFields,
  onChange,
}: {
  args: Record<string, unknown>;
  editableArgs: Record<string, string>;
  missingFields: string[];
  onChange: (key: string, value: string) => void;
}) {
  return (
    <div className="transaction-editor">
      <div className="transaction-editor-summary">
        <strong>{stringValue(args.name) || stringValue(args.ticker)}</strong>
        <span>{stringValue(args.ticker)} · {stringValue(args.side)}</span>
      </div>
      {missingFields.length ? (
        <div className="transaction-editor-alert">请补全 {missingFields.map(fieldLabel).join("、")} 后确认。</div>
      ) : (
        <div className="transaction-editor-hint">交易时间默认使用当前本地时间，可按实际成交时间修改。</div>
      )}
      <div className="transaction-editor-grid">
        {["quantity", "price", "datetime", "fee", "tax"].map((key) => (
          <label key={key} className="transaction-editor-field">
            <span>{fieldLabel(key)}</span>
            <input
              value={editableArgs[key] ?? ""}
              type={key === "datetime" ? "datetime-local" : "number"}
              step={key === "quantity" ? "1" : "0.01"}
              placeholder={key === "datetime" ? "默认当前时间" : "0"}
              onChange={(event) => onChange(key, event.target.value)}
            />
          </label>
        ))}
      </div>
      <details className="transaction-editor-raw">
        <summary>查看原始参数</summary>
        <pre>{JSON.stringify(args, null, 2)}</pre>
      </details>
    </div>
  );
}

const requiredTransactionFields = ["quantity", "price", "datetime"] as const;

function initialEditableArgs(args: Record<string, unknown>, capabilities: CapabilityModule[] = EXTERNAL_CAPABILITY_FALLBACK): Record<string, string> {
  const result: Record<string, string> = {};
  for (const key of ["quantity", "price", "datetime", "fee", "tax"]) {
    if (key === "datetime") {
      result[key] = hasValue(args[key]) ? toDatetimeLocalValue(String(args[key])) : currentDatetimeLocalValue();
    } else {
      result[key] = hasValue(args[key]) ? String(args[key]) : "";
    }
  }
  for (const key of ["subject", "subject_type", "depth", "user_goal", "research_goal", "subject_hint", "scope_hint", "budget_profile", "constraints"]) {
    if (hasValue(args[key])) result[key] = String(args[key]);
  }
  result.research_goal = result.research_goal || result.user_goal || "";
  result.budget_profile = result.budget_profile || result.depth || "standard";
  const enabledExternalTools = capabilities.filter((module) => module.enabled).flatMap((module) => module.tools);
  const allowed = arrayStringValue(args.allowed_tools, enabledExternalTools);
  const blocked = arrayStringValue(args.blocked_tools, []);
  for (const module of capabilities) {
    const moduleEnabled = module.enabled && module.tools.some((tool) => allowed.includes(tool)) && !module.tools.every((tool) => blocked.includes(tool));
    result[`module:${module.id}`] = moduleEnabled ? "on" : "off";
  }
  return result;
}

function mergeCapabilitySwitches(args: Record<string, unknown>, capabilities: CapabilityModule[], current: Record<string, string>): Record<string, string> {
  const moduleSwitches = initialEditableArgs(args, capabilities);
  const next = { ...current };
  for (const module of capabilities) {
    next[`module:${module.id}`] = moduleSwitches[`module:${module.id}`] ?? (module.enabled ? "on" : "off");
  }
  return next;
}

function buildArguments(original: Record<string, unknown>, edits: Record<string, string>): Record<string, unknown> {
  const next: Record<string, unknown> = { ...original };
  for (const key of ["quantity", "price", "fee", "tax"]) {
    if (key in edits) {
      const value = edits[key].trim();
      if (value === "") {
        delete next[key];
      } else {
        next[key] = Number(value);
      }
    }
  }
  if ("datetime" in edits) {
    const value = edits.datetime.trim();
    if (value) {
      next.datetime = toBackendDatetimeValue(value);
    } else {
      delete next.datetime;
    }
  }
  return next;
}

function getMissingTransactionFields(args: Record<string, unknown>): string[] {
  return requiredTransactionFields.filter((key) => {
    if (key === "quantity" || key === "price") {
      const value = Number(args[key]);
      return !Number.isFinite(value) || value <= 0;
    }
    return !hasValue(args[key]);
  });
}

function buildResearchArguments(original: Record<string, unknown>, edits: Record<string, string>, capabilities: CapabilityModule[]): Record<string, unknown> {
  const next: Record<string, unknown> = { ...original };
  for (const key of ["subject", "subject_type", "depth", "user_goal", "research_goal", "subject_hint", "scope_hint", "budget_profile", "constraints"]) {
    if (key in edits) next[key] = edits[key].trim();
  }
  const policy = researchToolPolicy(edits, capabilities.length ? capabilities : EXTERNAL_CAPABILITY_FALLBACK);
  next.allowed_tools = Array.from(policy.allowed);
  next.blocked_tools = Array.from(policy.blocked);
  next.depth = String(next.budget_profile || next.depth || "standard");
  next.user_goal = String(next.research_goal || next.user_goal || "");
  return next;
}

function getMissingResearchFields(args: Record<string, unknown>): string[] {
  return ["subject", "research_goal"].filter((key) => !hasValue(args[key]));
}

function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function fieldLabel(key: string): string {
  if (key === "quantity") return "数量";
  if (key === "price") return "价格";
  if (key === "datetime") return "时间";
  if (key === "fee") return "手续费";
  if (key === "tax") return "税费";
  return key;
}

function researchFieldLabel(key: string): string {
  if (key === "subject") return "研究对象";
  if (key === "user_goal" || key === "research_goal") return "研究目标";
  if (key === "subject_type") return "对象类型";
  if (key === "depth") return "研究深度";
  return key;
}

function researchSubjectTypeLabel(value: string): string {
  if (value === "stock") return "个股";
  if (value === "mainline") return "主线";
  if (value === "market") return "市场";
  if (value === "comparison") return "对比";
  return "待确认";
}

function researchDepthLabel(value: string): string {
  if (value === "quick") return "快速";
  if (value === "deep") return "深度";
  return "标准";
}

function currentDatetimeLocalValue(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function toDatetimeLocalValue(value: string): string {
  const trimmed = value.trim();
  const match = trimmed.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})/);
  if (match) return `${match[1]}T${match[2]}:${match[3]}`;
  const parsed = new Date(trimmed);
  if (!Number.isNaN(parsed.getTime())) {
    const pad = (part: number) => String(part).padStart(2, "0");
    return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
  }
  return currentDatetimeLocalValue();
}

function toBackendDatetimeValue(value: string): string {
  const normalized = value.replace("T", " ").trim();
  return normalized.length === 16 ? `${normalized}:00` : normalized;
}

const EXTERNAL_CAPABILITY_FALLBACK: CapabilityModule[] = [
  {
    id: "tradingagents",
    name: "个股深研",
    visibility: "external",
    description: "单标的多 Agent 深度研究能力",
    best_for: ["个股深度研究", "公司基本面", "交易视角"],
    tools: ["run_stock_research"],
    permissions: ["long_running", "expensive"],
    available_permissions: ["long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
  },
  {
    id: "bettafish",
    name: "主线雷达",
    visibility: "external",
    description: "市场主线与题材研究引擎",
    best_for: ["市场主线", "题材扩散", "产业链线索"],
    tools: ["run_market_discovery"],
    permissions: ["long_running", "expensive"],
    available_permissions: ["long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
  },
  {
    id: "tradinggraph",
    name: "产业链透视",
    visibility: "external",
    description: "产业链图谱与瓶颈分析能力",
    best_for: ["产业链图谱", "瓶颈节点", "主线结构"],
    tools: ["control_industry_graph", "read_industry_graph", "read_industry_graph_node"],
    permissions: ["long_running", "expensive"],
    available_permissions: ["long_running", "expensive"],
    enabled: true,
    timeout_seconds: 3600,
    default_timeout_seconds: 3600,
  },
];

function researchToolPolicy(edits: Record<string, string>, capabilities: CapabilityModule[]): { allowed: Set<string>; blocked: Set<string> } {
  const allowed = new Set<string>();
  const blocked = new Set<string>();
  for (const module of capabilities) {
    const value = edits[`module:${module.id}`];
    if (value === "off") {
      module.tools.forEach((tool) => blocked.add(tool));
    } else {
      module.tools.forEach((tool) => allowed.add(tool));
    }
  }
  return { allowed, blocked };
}

function arrayStringValue(value: unknown, fallback: string[]): string[] {
  if (!Array.isArray(value)) return fallback;
  return value.map((item) => String(item)).filter(Boolean);
}
