import { useEffect, useState } from "react";
import type { PendingAction } from "../types";

type Props = {
  action: PendingAction;
  onConfirm: (actionId: string, approved: boolean, argumentsOverride?: Record<string, unknown>) => Promise<void>;
};

export function PendingActionCard({ action, onConfirm }: Props) {
  const [state, setState] = useState<"pending" | "running" | "error">("pending");
  const [error, setError] = useState("");
  const copy = approvalCopy(action);

  useEffect(() => {
    setState("pending");
    setError("");
  }, [action.action_id]);

  async function handleConfirm(approved: boolean) {
    setState("running");
    setError("");
    try {
      await onConfirm(action.action_id, approved);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setState("error");
    }
  }

  return (
    <div className="pending-action approval-card">
      <div className="approval-compact-main">
        <div className="approval-compact-copy">
          <div className="approval-eyebrow">等待确认</div>
          <h3>{copy.title}</h3>
          <p>{copy.summary}</p>
          {action.reason ? <span>{action.reason}</span> : null}
          {error ? <strong className="approval-error">确认失败：{error}</strong> : null}
        </div>
        <span className={`approval-risk approval-risk--${action.risk || "medium"}`}>{riskLabel(action.risk)}</span>
      </div>
      <div className="pending-buttons approval-actions">
        <button type="button" className="primary" disabled={state === "running"} onClick={() => handleConfirm(true)}>
          {state === "running" ? "处理中" : copy.confirmText}
        </button>
        <button type="button" disabled={state === "running"} onClick={() => handleConfirm(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

function approvalCopy(action: PendingAction): { title: string; summary: string; confirmText: string } {
  const args = action.arguments || {};
  const ticker = firstString(args.ticker, args.symbol, args.stock_code, args.code);
  const name = firstString(args.name, args.stock_name, args.company_name);
  const subject = firstString(args.subject, args.research_goal, args.user_goal, args.query);
  const label = [name, ticker].filter(Boolean).join(" · ");

  if (action.tool_name === "start_research_thread") {
    return {
      title: "开启深度研究",
      summary: compact(subject || "确认后将创建后台深度研究线程。"),
      confirmText: "开始",
    };
  }
  if (action.tool_name === "run_stock_research") {
    return {
      title: "运行个股深研",
      summary: compact(label || subject || "确认后将生成单标的研究报告。"),
      confirmText: "运行",
    };
  }
  if (action.tool_name === "run_market_discovery") {
    return {
      title: "运行主线雷达",
      summary: compact(subject || "确认后将扫描市场主线与题材线索。"),
      confirmText: "运行",
    };
  }
  if (action.tool_name === "record_portfolio_transaction") {
    const side = sideLabel(firstString(args.side, args.action));
    const quantity = firstString(args.quantity, args.amount, args.shares);
    const price = firstString(args.price, args.cost_price);
    return {
      title: "记录交易",
      summary: compact([side, label, quantity ? `${quantity}股` : "", price ? `价格 ${price}` : ""].filter(Boolean).join(" · ")),
      confirmText: "记录",
    };
  }
  if (action.tool_name === "upsert_position") {
    const quantity = firstString(args.quantity, args.shares, args.volume);
    const cost = firstString(args.cost_price, args.avg_cost, args.price);
    return {
      title: "更新持仓",
      summary: compact([label || ticker || name, quantity ? `${quantity}股` : "", cost ? `成本 ${cost}` : ""].filter(Boolean).join(" · ")),
      confirmText: "更新",
    };
  }
  if (action.tool_name === "remove_position") {
    return {
      title: "删除持仓",
      summary: compact(label || ticker || name || "确认后将从本地持仓中删除该项。"),
      confirmText: "删除",
    };
  }
  if (action.tool_name === "add_watchlist_item") {
    return {
      title: "加入观察",
      summary: compact(label || subject || "确认后将加入观察列表。"),
      confirmText: "加入",
    };
  }
  if (action.tool_name === "remove_watchlist_item") {
    return {
      title: "移出观察",
      summary: compact(label || ticker || name || "确认后将从观察列表移除。"),
      confirmText: "移除",
    };
  }
  return {
    title: "确认操作",
    summary: compact(subject || label || action.tool_name),
    confirmText: "确认",
  };
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function compact(value: string): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > 120 ? `${text.slice(0, 118)}...` : text;
}

function sideLabel(value: string): string {
  const normalized = value.toLowerCase();
  if (["buy", "bought", "add", "increase", "买入", "加仓"].includes(normalized)) return "买入";
  if (["sell", "sold", "reduce", "decrease", "卖出", "减仓"].includes(normalized)) return "卖出";
  return value || "交易";
}

function riskLabel(risk: string | undefined): string {
  if (risk === "high") return "高风险";
  if (risk === "low") return "低风险";
  return "需确认";
}
