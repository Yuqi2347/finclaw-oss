import { useEffect, useState } from "react";
import { cancelAnalysisJob, getAnalysisJob } from "../api/finclaw";
import type { AnalysisJob } from "../types";

const STOCK_RESEARCH_STAGE_ORDER: Array<[string, string]> = [
  ["market", "技术分析"],
  ["social", "情绪分析"],
  ["news", "新闻舆情"],
  ["fundamentals", "基本面"],
  ["policy", "政策分析"],
  ["quality_gate", "质量门控"],
  ["debate", "多空辩论"],
  ["trader", "交易决策"],
  ["risk", "风控评估"],
  ["pm", "最终决策"],
];

const MARKET_DISCOVERY_STAGE_ORDER: Array<[string, string]> = [
  ["mindspider", "MindSpider"],
  ["query", "QueryEngine"],
  ["media", "MediaEngine"],
  ["insight", "InsightEngine"],
  ["forum", "ForumEngine"],
  ["structure", "结构化汇总"],
  ["report", "ReportEngine"],
];

type Props = {
  jobs: AnalysisJob[];
  onUpdate: (job: AnalysisJob) => void;
  onDismiss: (jobId: string) => void;
  inline?: boolean;
};

export function AnalysisJobDock({ jobs, onUpdate, onDismiss, inline = false }: Props) {
  const visible = jobs.filter((job) => job.status === "running" || job.status === "cancelling" || job.status === "failed" || job.status === "cancelled");
  const [minimized, setMinimized] = useState(false);

  useEffect(() => {
    if (!visible.length) return;
    const id = window.setInterval(async () => {
      for (const job of visible) {
        try {
          onUpdate(await getAnalysisJob(job.job_id));
        } catch {
          // Keep the last known job state.
        }
      }
    }, 1500);
    return () => window.clearInterval(id);
  }, [visible, onUpdate]);

  if (!visible.length) return null;

  if (minimized) {
    const runningCount = visible.filter((job) => job.status === "running" || job.status === "cancelling").length;
    const failedCount = visible.filter((job) => job.status === "failed").length;
    return (
      <button className={`job-dock-mini${inline ? " inline" : ""}`} onClick={() => setMinimized(false)}>
        分析任务 {runningCount ? `${runningCount} 运行中` : ""}{failedCount ? ` ${failedCount} 失败` : ""}
      </button>
    );
  }

  return (
    <div className={`job-dock${inline ? " inline" : ""}`}>
      <div className="job-dock-toolbar">
        <span>后台分析</span>
        <button onClick={() => setMinimized(true)}>缩小</button>
      </div>
      {visible.slice(0, 3).map((job) => (
        <JobCard key={job.job_id} job={job} onDismiss={onDismiss} onUpdate={onUpdate} />
      ))}
    </div>
  );
}

function JobCard({
  job,
  onDismiss,
  onUpdate,
}: {
  job: AnalysisJob;
  onDismiss: (jobId: string) => void;
  onUpdate: (job: AnalysisJob) => void;
}) {
  const [open, setOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const stages = normalizedStages(job);
  const running = job.status === "running" || job.status === "cancelling";
  const failed = job.status === "failed";
  const cancelled = job.status === "cancelled";
  const latest = job.progress_log[job.progress_log.length - 1];
  const subject = typeof job.args.ticker === "string" ? job.args.ticker : "A股市场";
  const cancelJob = async () => {
    setCancelling(true);
    try {
      onUpdate(await cancelAnalysisJob(job.job_id));
    } finally {
      setCancelling(false);
    }
  };
  return (
    <div className={`job-card ${failed ? "failed" : cancelled ? "cancelled" : running ? "running" : "done"}`}>
      <div className="job-card-head">
        <div>
          <div className="job-title">{job.job_type === "stock_research" ? "个股深度研究" : "市场主线分析"}</div>
          <div className="job-subtitle">{subject} · {stageLabel(job.current_stage)}</div>
        </div>
        <div className="job-actions">
          <button onClick={() => setOpen((value) => !value)}>{open ? "收起" : "日志"}</button>
          {running && <button className="ghost" disabled={cancelling} onClick={cancelJob}>{cancelling ? "取消中" : "取消"}</button>}
          {failed && <button className="ghost" onClick={() => onDismiss(job.job_id)}>删除</button>}
          {cancelled && <button className="ghost" onClick={() => onDismiss(job.job_id)}>删除</button>}
        </div>
      </div>
      <div className="job-progress">
        <span style={{ width: `${progressPercent(job)}%` }} />
      </div>
      <div className="job-meta">
        <span>{cancelled ? "已取消" : failed ? "运行失败" : running ? "运行中" : "已完成"}</span>
        {job.output_report_id && <span>{job.output_report_id}</span>}
      </div>
      {stages.length ? (
        <div className="job-stages">
          {stages.map((stage) => (
            <span key={stage.id} className={`job-stage ${stage.status}`}>
              {stage.name}
            </span>
          ))}
        </div>
      ) : null}
      {job.error && <div className="job-error">{job.error}</div>}
      {latest && <div className="job-latest">{latest}</div>}
      {open && (
        <pre className="job-log">{job.progress_log.slice(-10).join("\n") || "暂无日志"}</pre>
      )}
    </div>
  );
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    starting: "启动中",
    process_started: "进程启动",
    running: "运行中",
    mindspider: "MindSpider",
    query: "QueryEngine",
    media: "MediaEngine",
    insight: "InsightEngine",
    forum: "ForumEngine",
    structure: "结构化汇总",
    report: "ReportEngine",
    market: "技术分析",
    social: "情绪分析",
    news: "新闻舆情",
    fundamentals: "基本面",
    policy: "政策分析",
    hot_money: "游资追踪",
    lockup: "解禁监控",
    quality_gate: "质量门控",
    debate: "多空辩论",
    trader: "交易决策",
    risk: "风控评估",
    pm: "最终决策",
    completed: "已完成",
    failed: "失败",
    cancelling: "取消中",
    cancelled: "已取消",
  };
  return labels[stage] ?? stage;
}

function progressPercent(job: AnalysisJob): number {
  const stages = normalizedStages(job);
  if (job.status === "failed" || job.status === "completed" || job.status === "cancelled") return 100;
  if (stages.length) {
    const done = stages.filter((stage) => stage.status === "done").length;
    const active = stages.some((stage) => stage.status === "active") ? 0.5 : 0;
    return Math.max(12, Math.min(92, Math.round(((done + active) / stages.length) * 100)));
  }
  const order = ["技术分析", "情绪分析", "新闻舆情", "基本面", "政策分析", "游资追踪", "解禁监控", "质量门控", "多空辩论", "交易决策", "风控评估", "最终决策"];
  const index = order.indexOf(stageLabel(job.current_stage));
  if (index < 0) return 12;
  return Math.max(12, Math.min(92, Math.round(((index + 1) / order.length) * 100)));
}

function normalizedStages(job: AnalysisJob): Array<{ id: string; name: string; status: string }> {
  const stageOrder = job.job_type === "market_discovery" ? MARKET_DISCOVERY_STAGE_ORDER : job.job_type === "stock_research" ? STOCK_RESEARCH_STAGE_ORDER : [];
  if (!stageOrder.length) return job.stages ?? [];

  const canonicalStageIds = new Set(stageOrder.map(([stageId]) => stageId));
  const existingRows = (job.stages ?? []).filter((row) => canonicalStageIds.has(row.id));
  const completed = new Set(existingRows.filter((row) => row.status === "done").map((row) => row.id));
  let activeStage = canonicalStageIds.has(job.current_stage)
    ? job.current_stage
    : existingRows.find((row) => row.status === "active")?.id ?? "";

  if (job.status === "completed") {
    activeStage = "completed";
    for (const [stageId] of stageOrder) completed.add(stageId);
  } else if (canonicalStageIds.has(activeStage)) {
    for (const [stageId] of stageOrder) {
      if (stageId === activeStage) break;
      completed.add(stageId);
    }
  }

  return stageOrder.map(([stageId, name]) => {
    let status = "pending";
    if (activeStage === "completed" || completed.has(stageId)) {
      status = "done";
    } else if (stageId === activeStage) {
      status = "active";
    }
    return { id: stageId, name, status };
  });
}
