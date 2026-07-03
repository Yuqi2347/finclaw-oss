export type Role = "assistant" | "user";

export interface PendingAction {
  action_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  permission: string;
  risk?: string;
  risk_reason?: string;
  reason: string;
  status: string;
  created_at: string;
}

export interface ApprovalQueue {
  session_id: string;
  active_action: PendingAction | null;
  queued_actions: PendingAction[];
  queue_size: number;
}

export interface ReportLink {
  title: string;
  view_url: string;
  download_url: string;
}

export interface WebSource {
  source_id: string;
  marker?: number;
  title: string;
  url: string;
  domain?: string;
  published_at?: string | null;
  credibility?: string;
  excerpt?: string;
  provider?: string;
}

export interface Citation {
  marker: number;
  source_id: string;
  claim_text?: string;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  active_run_id?: string | null;
  active_approval_action_id?: string | null;
  message_count: number;
  last_role?: Role | null;
  last_content?: string | null;
  last_message_at?: string | null;
}

export interface ToolCallRecord {
  tool?: string;
  result?: unknown;
}

export interface AttachmentMeta {
  attachment_id: string;
  session_id?: string | null;
  type: "image" | string;
  mime_type: string;
  size?: number | null;
  width?: number | null;
  height?: number | null;
  thumb_url?: string | null;
  view_url?: string | null;
  created_at?: string | null;
  referenced?: boolean;
}

export interface AnalysisJob {
  job_id: string;
  job_type: string;
  status: string;
  current_stage: string;
  args: Record<string, unknown>;
  stages?: Array<{ id: string; name: string; status: string }>;
  progress_log: string[];
  output_report_id?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface CapabilityModule {
  id: string;
  name: string;
  display_name?: string;
  english_name?: string;
  aliases?: string[];
  visibility: string;
  category?: string;
  description: string;
  best_for: string[];
  skill?: string;
  tools: string[];
  permissions: string[];
  available_permissions: string[];
  enabled: boolean;
  timeout_seconds: number;
  default_timeout_seconds: number;
  implementation?: Record<string, unknown>;
  health?: {
    status?: string;
    message?: string;
  };
}

export interface ChatMessage {
  id: string;
  serverId?: number;
  role: Role;
  content: string;
  attachments?: AttachmentMeta[];
  pendingActions?: PendingAction[];
  toolCalls?: ToolCallRecord[];
  reportLinks?: ReportLink[];
  sources?: WebSource[];
  citations?: Citation[];
}

export interface StoredChatMessage {
  message_id: number;
  session_id: string;
  role: Role;
  content: string;
  attachments?: AttachmentMeta[];
  tool_calls: ToolCallRecord[];
  report_links: ReportLink[];
  sources?: WebSource[];
  citations?: Citation[];
  created_at: string;
}

export interface StreamState {
  text: string;
  status: string;
  pendingActions: PendingAction[];
  toolCalls: ToolCallRecord[];
}

export interface MarketMetricSnapshot {
  symbol?: string;
  name?: string;
  category?: string;
  price?: number | null;
  change_pct?: number | null;
  updated_at?: string | null;
}

export interface PortfolioSummary {
  watchlist_count?: number;
  position_count?: number;
  trigger_count?: number;
  last_updated?: string | null;
}

export interface PortfolioPositionSnapshot {
  ticker?: string;
  name?: string;
  quantity?: number | null;
  cost_price?: number | null;
  current_price?: number | null;
  day_change_pct?: number | null;
  day_change_amount?: number | null;
  day_pnl?: number | null;
  market_value?: number | null;
  cost_value?: number | null;
  pnl?: number | null;
  pnl_pct?: number | null;
  note?: string | null;
  updated_at?: string | null;
}

export interface PortfolioPerformanceSnapshot {
  total_pnl?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  win_rate?: number | null;
  trade_count?: number | null;
  recent_trades?: Array<Record<string, unknown>>;
  updated_at?: string | null;
  basis_note?: string | null;
}

export interface WatchlistCardPoint {
  date?: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
}

export interface WatchlistCardSnapshot {
  ticker?: string;
  name?: string;
  current_price?: number | null;
  change_pct?: number | null;
  change_amount?: number | null;
  updated_at?: string | null;
  time_context?: Record<string, unknown> | null;
  five_day_return_pct?: number | null;
  five_day_series?: WatchlistCardPoint[];
}

export interface DashboardNewsItem {
  id?: string | number | null;
  title?: string;
  summary?: string;
  url?: string;
  source_platform?: string;
  source_label?: string;
  published_at_text?: string;
  snapshot_date?: string;
  rank_position?: number | null;
  detail_level?: string | null;
  category?: string | null;
  event_type?: string | null;
  tags?: string[];
  confidence?: number | null;
  final_score?: number | null;
}

export interface DashboardNewsMeta {
  snapshot_date?: string | null;
  updated_at?: string | null;
  refreshing?: boolean;
  last_refresh_requested_at?: string | null;
  last_refresh_finished_at?: string | null;
  last_refresh_error?: string | null;
  item_count?: number | null;
  error?: string | null;
}

export interface MarketSidebarPayload {
  updated_at?: string;
  portfolio_summary?: PortfolioSummary | PortfolioPositionSnapshot[] | Record<string, unknown>;
  portfolio_performance?: PortfolioPerformanceSnapshot | Record<string, unknown>;
  market_overview?: {
    indices?: MarketMetricSnapshot[];
  } | Record<string, unknown>;
  watchlist?: Array<Record<string, unknown>>;
  watchlist_cards?: WatchlistCardSnapshot[];
  positions?: Array<Record<string, unknown>>;
  news?: DashboardNewsItem[];
  news_meta?: DashboardNewsMeta | Record<string, unknown>;
  data_source_status?: Record<string, unknown>;
  errors?: string[];
}

export type DashboardSidebarPayload = MarketSidebarPayload;

export interface LlmLogSummary {
  id: number;
  trace_id?: string | null;
  session_id?: string | null;
  run_id?: string | null;
  model: string;
  base_url: string;
  tool_choice?: unknown;
  temperature?: number | null;
  status: string;
  error?: string | null;
  started_at: string;
  completed_at?: string | null;
  duration_ms?: number | null;
  first_token_ms?: number | null;
  request_tokens_estimate?: number | null;
  response_tokens_estimate?: number | null;
  total_tokens_estimate?: number | null;
  request_chars?: number | null;
  response_chars?: number | null;
}

export interface LlmLogDetail extends LlmLogSummary {
  request?: Record<string, unknown>;
  response?: Record<string, unknown>;
}

export interface ResearchPlanStep {
  step_id?: string;
  question?: string;
  status?: string;
  conclusion?: string;
  tool_type?: string;
  expected_evidence?: string;
  stop_condition?: string;
  risk?: string;
  updated_at?: string;
}

export interface ResearchThread {
  thread_id: string;
  record_id?: string;
  session_id: string;
  subject: string;
  subject_type: string;
  depth: string;
  status: string;
  user_goal?: string;
  plan?: ResearchPlanStep[];
  rounds?: Array<{
    round: number;
    focus?: string;
    status?: string;
    summary?: string;
    updated_at?: string;
    validator_status?: string;
    validator_confidence?: string;
    tools?: Array<{
      tool?: string;
      status?: string;
      elapsed_ms?: number;
      summary?: string;
      finished_at?: string;
    }>;
  }>;
  validator?: {
    status?: string;
    confidence?: string;
    reason?: string;
    missing_analysis?: string[];
    overclaims?: string[];
    next_feedback?: string;
  };
  metrics?: {
    wall_elapsed_ms?: number;
    total_step_elapsed_ms?: number;
    slowest_steps?: Array<{ step_id?: string; tool_type?: string; status?: string; elapsed_ms?: number }>;
    web_runs?: Array<{
      batch?: number;
      query_count?: number;
      total_source_budget?: number;
      max_sources_per_query?: number;
      status?: string;
      stopped_reason?: string;
      source_count?: number;
      elapsed_ms?: number;
      queries?: string[];
    }>;
    budget?: Record<string, number>;
  } | Record<string, unknown>;
  current_conclusion?: string;
  error?: string | null;
  truncated?: {
    round_total?: number;
  };
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface ResearchRecordSummary {
  record_id: string;
  title: string;
  subject_type?: string;
  updated_at?: string;
  latest_thread_id?: string | null;
  user_goal?: string;
  core_conclusion?: string;
  gap_count?: number;
  validator_status?: string | null;
  quality_level?: string;
  sections?: Array<{ section: string; level?: number; chars?: number }>;
  file_size?: number;
}

export interface ResearchRecordReadWindow {
  offset: number;
  max_chars: number;
  returned_chars: number;
  total_chars: number;
  has_more: boolean;
  next_offset?: number | null;
  content: string;
}

export interface ResearchRecordDetail {
  success: boolean;
  record: ResearchRecordSummary;
  read_window: ResearchRecordReadWindow;
}
