import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { getDashboardSidebar, streamRefreshDashboardSidebar } from "../api/finclaw";
import type {
  DashboardSidebarPayload,
} from "../types";

type MarketRecord = Record<string, unknown>;

type SummarySnapshot = {
  marketValue: number | null;
  pnl: number | null;
};

const SIDEBAR_CACHE_KEY = "finclaw.dashboard.sidebar.v4";

export function MarketSidebar() {
  const cachedPayloadRef = useRef<DashboardSidebarPayload | null>(readSidebarCache());
  const [data, setData] = useState<DashboardSidebarPayload | null>(cachedPayloadRef.current);
  const [loading, setLoading] = useState(cachedPayloadRef.current == null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshProgress, setRefreshProgress] = useState<string>("");
  const [refreshPercentage, setRefreshPercentage] = useState<number>(0);
  const [bootstrapped, setBootstrapped] = useState(cachedPayloadRef.current != null);
  const [error, setError] = useState("");
  const [showNews, setShowNews] = useState(false);
  const requestSeq = useRef(0);
  const mountedRef = useRef(true);
  const refreshingRef = useRef(false);

  const loadSidebar = async (mode: "initial" | "poll" | "manual" = "poll") => {
    const seq = ++requestSeq.current;
    if (mode === "initial" && !bootstrapped) setLoading(true);
    if (mode === "manual") {
      refreshingRef.current = true;
      setRefreshing(true);
      setRefreshProgress("准备刷新...");
      setRefreshPercentage(0);

      // 使用流式刷新
      try {
        await streamRefreshDashboardSidebar({
          onSidebarData: (payload) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            setData(payload);
            writeSidebarCache(payload);
            setError("");
            setBootstrapped(true);
          },
          onRefreshStarted: (data) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            setRefreshProgress("正在刷新左栏快照...");
            setRefreshPercentage(0);
          },
          onRefreshProgress: (data) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            if (data.stage === "news") {
              const message = data.message ?? "新闻快照后台刷新中...";
              setRefreshProgress(data.status === "error" ? `新闻刷新失败: ${data.error}` : message);
            } else if (data.stage === "market_indices") {
              if (data.status === "error") {
                setRefreshProgress(`核心指数刷新失败: ${data.error}`);
              } else {
                setRefreshProgress(data.message ?? "核心指数刷新中...");
              }
            } else if (data.stage === "snapshot" && data.ticker) {
              // 不显示单个标的进度，保留整体快照刷新提示。
              if (data.percentage !== undefined) {
                setRefreshPercentage(data.percentage);
              }
            }
          },
          onRefreshCompleted: (data) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            setRefreshProgress("刷新完成");
            setRefreshPercentage(100);
            setTimeout(() => {
              if (mountedRef.current && seq === requestSeq.current) {
                setRefreshProgress("");
                setRefreshPercentage(0);
              }
            }, 2000);
          },
          onRefreshFailed: (data) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            setError(`刷新失败: ${data.error}`);
            setRefreshProgress("");
            setRefreshPercentage(0);
          },
          onRefreshWarning: (data) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            console.warn("Refresh warning:", data.message);
          },
          onError: (message) => {
            if (!mountedRef.current || seq !== requestSeq.current) return;
            setError(message);
            setRefreshProgress("");
            setRefreshPercentage(0);
          },
        });
      } catch (err) {
        if (!mountedRef.current || seq !== requestSeq.current) return;
        setError(err instanceof Error ? err.message : "刷新失败");
        setRefreshProgress("");
        setRefreshPercentage(0);
      } finally {
        if (!mountedRef.current || seq !== requestSeq.current) return;
        refreshingRef.current = false;
        setRefreshing(false);
      }
      return;
    }

    // 非手动刷新，使用原有逻辑
    try {
      const payload = await getDashboardSidebar();
      if (!mountedRef.current || seq !== requestSeq.current) return;
      setData(payload);
      writeSidebarCache(payload);
      setError("");
      setBootstrapped(true);
    } catch (err) {
      if (!mountedRef.current || seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : "市场看板加载失败");
      setBootstrapped(true);
    } finally {
      if (!mountedRef.current || seq !== requestSeq.current) return;
      if (mode === "initial") setLoading(false);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    let stopped = false;

    void loadSidebar("initial");
    const timer = window.setInterval(() => {
      if (!stopped && !refreshingRef.current) void loadSidebar("poll");
    }, 30000);
    return () => {
      mountedRef.current = false;
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  const overview = asRecord(data?.market_overview);
  const allIndices = asRecordArray(overview?.indices);

  // 固定的6个核心指数，按指定顺序显示
  const domesticIndices = useMemo(() => {
    const targetNames = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '中证1000'];
    const indexMap = new Map<string, Record<string, any>>();

    // 建立名称到数据的映射
    allIndices
      .filter(item => stringValue(item, "category") !== "international_index")
      .forEach(item => {
        const name = stringValue(item, "name");
        if (targetNames.includes(name)) {
          indexMap.set(name, item);
        }
      });

    // 按固定顺序返回，如果某个指数没有数据则返回占位对象
    return targetNames.map(name =>
      indexMap.get(name) || { name, symbol: "", price: null, change_pct: null }
    );
  }, [allIndices]);

  const newsItems = asRecordArray(data?.news).slice(0, 9);
  const newsMeta = asRecord(data?.news_meta);
  const allWatchlist = asRecordArray(data?.watchlist);
  const watchlistCards = Array.isArray(data?.watchlist_cards) ? data.watchlist_cards.filter(isRecord) : [];
  const allPositions = asRecordArray(data?.positions);
  const portfolioRows = asRecordArray(data?.portfolio_summary);
  const portfolioPerformance = asRecord(data?.portfolio_performance) ?? {};

  // 观察列表去重：排除已在持仓中的标的
  const positionTickers = new Set(allPositions.map(item => stringValue(item, "ticker")));
  const watchlist = allWatchlist.filter(item => !positionTickers.has(stringValue(item, "ticker"))).slice(0, 4);
  const renderedWatchlist = watchlistCards.length
    ? watchlistCards.filter(item => !positionTickers.has(stringValue(item, "ticker"))).slice(0, 4)
    : watchlist;
  const positions = allPositions.slice(0, 4);

  const holdings = portfolioRows.length ? portfolioRows : positions;
  const portfolio = useMemo(() => summarizePortfolio(holdings), [holdings]);

  const heroStamp = !bootstrapped && loading ? "同步中" : refreshing ? "刷新中" : formatStamp(data?.updated_at);
  const newsSnapshotDate = newsMeta ? stringValue(newsMeta, "snapshot_date") : "";
  const newsHint = Boolean(newsMeta?.refreshing)
    ? "新闻更新中"
    : newsSnapshotDate
      ? `快照 ${newsSnapshotDate}`
      : "DataHub";
  const dataSourceStatus = asRecord(data?.data_source_status);
  const dataSourceState = dataSourceStatus ? stringValue(dataSourceStatus, "status") : "";
  const dataSourceNote = dataSourceStatus ? stringValue(dataSourceStatus, "note") : "";
  const dataSourceSnapshotAt = dataSourceStatus ? stringValue(dataSourceStatus, "latest_snapshot_at") : "";
  const showDataSourceNotice = Boolean(dataSourceState && dataSourceState !== "ok");

  return (
    <aside className="market-sidebar">
      <div className="sidebar-shell sidebar-shell--dashboard">
        <section className="sidebar-headerbar">
          <div className="sidebar-headerbar-main">
            <div className="sidebar-headerbar-copy">
              <h2>市场看板</h2>
            </div>
            <div className="sidebar-headerbar-actions">
              <div className="sidebar-headerbar-time">
                <span className="sidebar-headerbar-label">上次更新</span>
                <strong className="sidebar-headerbar-value">{heroStamp}</strong>
              </div>
              <button
                type="button"
                className="sidebar-headerbar-refresh"
                onClick={() => void loadSidebar("manual")}
                disabled={refreshing}
                title="刷新左栏数据"
                aria-busy={refreshing}
              >
                <span className={`refresh-icon ${refreshing ? "spinning" : ""}`}>↻</span>
                <span>{refreshing ? "刷新中" : "刷新"}</span>
              </button>
            </div>
          </div>

          {showDataSourceNotice && (
            <div className="sidebar-source-status">
              <strong>数据源降级</strong>
              <span>{dataSourceNote || "部分数据可能来自本地缓存"}</span>
              {dataSourceSnapshotAt ? <em>最近快照 {formatStamp(dataSourceSnapshotAt)}</em> : null}
            </div>
          )}

          {refreshing && refreshProgress && (
            <div className="refresh-progress-bar">
              <div className="refresh-progress-text">{refreshProgress}</div>
              {refreshPercentage > 0 && (
                <div className="refresh-progress-track">
                  <div
                    className="refresh-progress-fill"
                    style={{ width: `${refreshPercentage}%` }}
                  />
                </div>
              )}
            </div>
          )}
        </section>

        <section className="sidebar-card">
          <SectionHeader title="核心指数监控" hint="A股市场" />
          <div className="index-grid index-grid-2x6">
            {/* 固定6个核心指数，2行3列布局 */}
            {domesticIndices.map((item) => (
              <IndexCard key={stringValue(item, "name") || stringValue(item, "symbol")} item={item} />
            ))}
          </div>
        </section>

        <section className="sidebar-card">
          <SectionHeader title="资产概览" hint="本地记录" />
          <div className="asset-summary">
            <AssetStat label="持仓市值" value={formatAssetValue(portfolio.marketValue)} tone="asset-tone-ink" />
            <AssetStat label="持仓盈亏" value={formatPnlValue(portfolio.pnl)} tone={assetToneClass(portfolio.pnl)} />
          </div>

          <div className="asset-group">
            <div className="asset-group-title">持仓列表</div>
            {holdings.length ? holdings.map((item) => <HoldingRow key={stringKey(item, "ticker")} item={item} />) : <EmptyHint>暂无持仓</EmptyHint>}
          </div>

          <div className="asset-group">
            <div className="asset-group-title">观察列表</div>
            {renderedWatchlist.length ? renderedWatchlist.map((item) => <WatchlistRow key={stringKey(item, "ticker")} item={item} />) : <EmptyHint>暂无观察标的</EmptyHint>}
          </div>
        </section>

        <section className="sidebar-card">
          <SectionHeader title="交易绩效" hint="账本口径" />
          <PortfolioPerformanceCard snapshot={portfolioPerformance} />
        </section>

        <section className="sidebar-card sidebar-card--secondary">
          <button type="button" className="sidebar-collapse-head" onClick={() => setShowNews((value) => !value)}>
            <span>市场新闻</span>
            <em>{showNews ? "收起" : newsHint}</em>
          </button>
          {showNews && (
            <div className="news-list">
              {newsItems.length ? newsItems.map((item, index) => <NewsRow key={stringKey(item, "id") || `${stringKey(item, "title")}-${index}`} item={item} />) : <EmptyHint>暂无新闻快照</EmptyHint>}
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}

function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="sidebar-section-head">
      <h3>{title}</h3>
      {hint ? <span>{hint}</span> : null}
    </div>
  );
}

function IndexCard({ item, placeholder }: { item: MarketRecord; placeholder?: boolean }) {
  const name = stringValue(item, "name") || stringValue(item, "symbol") || "未知";
  const price = readOptionalNumber(item, "price");
  const changePct = readOptionalNumber(item, "change_pct");
  const tone = toneClass(changePct);

  return (
    <div className={`index-card ${placeholder ? "placeholder" : ""} ${tone}`}>
      <div className="index-card-name">{name}</div>
      <div className="index-card-price">{price !== null ? formatNumber(price) : "--"}</div>
      <div className={`index-card-change ${tone}`}>{formatPct(changePct)}</div>
    </div>
  );
}

function AssetStat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <article className="asset-stat-card">
      <div className="asset-stat-label">{label}</div>
      <div className={`asset-stat-value ${tone ?? "asset-tone-flat"}`}>{value}</div>
    </article>
  );
}

function PortfolioPerformanceCard({ snapshot }: { snapshot: MarketRecord }) {
  const totalPnl = readOptionalNumber(snapshot, "total_pnl");
  const realizedPnl = readOptionalNumber(snapshot, "realized_pnl");
  const unrealizedPnl = readOptionalNumber(snapshot, "unrealized_pnl");
  const winRate = readOptionalNumber(snapshot, "win_rate");
  const tradeCount = readOptionalNumber(snapshot, "trade_count");
  const recentTrades = Array.isArray(snapshot.recent_trades) ? snapshot.recent_trades.filter(isRecord).slice(0, 5) : [];

  return (
    <div className="performance-panel">
      <div className="performance-summary">
        <AssetStat label="总盈亏" value={formatPnlValue(totalPnl)} tone={assetToneClass(totalPnl)} />
        <AssetStat label="胜率" value={winRate === null ? "--" : `${(winRate * 100).toFixed(1)}%`} tone="asset-tone-ink" />
      </div>
      <div className="performance-breakdown">
        <div>
          <span>已实现</span>
          <strong className={assetToneClass(realizedPnl)}>{formatPnlValue(realizedPnl)}</strong>
        </div>
        <div>
          <span>未实现</span>
          <strong className={assetToneClass(unrealizedPnl)}>{formatPnlValue(unrealizedPnl)}</strong>
        </div>
        <div>
          <span>已平仓</span>
          <strong>{tradeCount === null ? "--" : `${formatNumber(tradeCount)} 笔`}</strong>
        </div>
      </div>
      <div className="asset-group">
        <div className="asset-group-title">近 5 次交易</div>
        {recentTrades.length ? recentTrades.map((item) => (
          <div className="performance-trade-row" key={stringKey(item, "transaction_id")}>
            <div>
              <strong>{stringValue(item, "name") || stringValue(item, "ticker")}</strong>
              <span>{stringValue(item, "side")} · {formatQuantity(readOptionalNumber(item, "quantity"))}</span>
            </div>
            <em className={assetToneClass(readOptionalNumber(item, "realized_pnl"))}>{formatPnlValue(readOptionalNumber(item, "realized_pnl"))}</em>
          </div>
        )) : <EmptyHint>暂无交易流水</EmptyHint>}
      </div>
    </div>
  );
}

function HoldingRow({ item }: { item: MarketRecord }) {
  const quantity = formatQuantity(readOptionalNumber(item, "quantity"));
  const costPrice = formatQuotePrice(readOptionalNumber(item, "cost_price"));
  const currentPrice = formatQuotePrice(readOptionalNumber(item, "current_price"));
  const dayChangePct = readOptionalNumber(item, "day_change_pct");
  const dayPnl = readOptionalNumber(item, "day_pnl");
  const marketValue = readOptionalNumber(item, "market_value");
  const pnl = readOptionalNumber(item, "pnl");
  const pnlPct = formatPct(readOptionalNumber(item, "pnl_pct"));
  const tone = holdingToneClass(pnl);
  return (
    <div className={`holding-row ${tone}`}>
      <div className="holding-row-top">
        <div className="holding-row-identity">
          <strong>{stringValue(item, "name") || stringValue(item, "ticker")}</strong>
          <span>{stringValue(item, "ticker")} · {quantity}</span>
        </div>
        <div className="holding-row-quote">
          <div className="holding-quote-price">{currentPrice}</div>
        </div>
      </div>

      <div className="holding-row-metrics">
        <div className="holding-inline-stat">
          <span>日内盈亏</span>
          <em className={assetToneClass(dayChangePct)}>{formatPct(dayChangePct)}</em>
          <strong className={assetToneClass(dayPnl)}>{formatPnlValue(dayPnl)}</strong>
        </div>
        <div className="holding-inline-stat">
          <span>持仓盈亏</span>
          <em className={assetToneClass(pnl)}>{pnlPct}</em>
          <strong className={assetToneClass(pnl)}>{formatPnlValue(pnl)}</strong>
        </div>
      </div>

      <div className="holding-row-meta">
        <div className="holding-meta-chip">
          <span>成本</span>
          <strong>{costPrice}</strong>
        </div>
        <div className="holding-meta-chip">
          <span>持仓市值</span>
          <strong>{formatAssetValue(marketValue)}</strong>
        </div>
      </div>
    </div>
  );
}

function WatchlistRow({ item }: { item: MarketRecord }) {
  const currentPrice = readOptionalNumber(item, "current_price");
  const changePct = readOptionalNumber(item, "change_pct");
  const series = Array.isArray(item.five_day_series) ? item.five_day_series.filter(isRecord) : [];
  return (
    <div className="watch-card">
      <div className="watch-card-left">
        <strong>{stringValue(item, "name") || stringValue(item, "ticker")}</strong>
        <span>{stringValue(item, "ticker")}</span>
      </div>
      <div className="watch-card-chart">
        <MiniCloseLineChart points={series} tone={assetToneClass(changePct)} />
      </div>
      <div className="watch-card-right">
        <strong className="watch-card-price">{formatQuotePrice(currentPrice)}</strong>
        <span className={assetToneClass(changePct)}>{formatPct(changePct)}</span>
      </div>
    </div>
  );
}

function MiniCloseLineChart({ points, tone }: { points: MarketRecord[]; tone: string }) {
  if (!points.length) {
    return <div className="watch-chart-empty">--</div>;
  }

  const normalized = points
    .map((point) => {
      const close = readOptionalNumber(point, "close");
      if (close == null) return null;
      return {
        close: close as number,
      };
    })
    .filter((point): point is { close: number } => point !== null);

  if (!normalized.length) {
    return <div className="watch-chart-empty">--</div>;
  }

  const maxClose = Math.max(...normalized.map((point) => point.close));
  const minClose = Math.min(...normalized.map((point) => point.close));
  const range = Math.max(maxClose - minClose, 0.0001);
  const width = 120;
  const height = 52;
  const stepX = normalized.length > 1 ? width / (normalized.length - 1) : width / 2;
  const chartPoints = normalized.map((point, index) => {
    const x = normalized.length > 1 ? stepX * index : width / 2;
    const y = ((maxClose - point.close) / range) * (height - 16) + 12;
    return { ...point, index, x, y };
  });
  const pointsAttr = chartPoints.map((point) => `${point.x},${point.y}`).join(" ");
  const lastPoint = chartPoints[chartPoints.length - 1];

  return (
    <svg className="watch-chart-svg" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <polyline className={`watch-line ${tone}`} points={pointsAttr} />
      <circle className={`watch-line-dot ${tone}`} cx={lastPoint.x} cy={lastPoint.y} r="2.8" />
    </svg>
  );
}

function NewsRow({ item }: { item: MarketRecord }) {
  const title = stringValue(item, "title") || "新闻";
  const summary = stringValue(item, "summary") || title;
  const url = stringValue(item, "url");
  const source = stringValue(item, "source_label") || stringValue(item, "source_platform") || "来源";
  const publishedAt = formatNewsPublishedText(stringValue(item, "published_at_text"));
  const rawTags = item["tags"];
  const tags = Array.isArray(rawTags)
    ? rawTags.filter((tag): tag is string => typeof tag === "string" && tag.trim().length > 0).slice(0, 3)
    : [];

  const body = (
    <>
      <div className="news-card-head">
        <strong>{title}</strong>
      </div>
      <div className="news-card-summary">{summary}</div>
      {tags.length ? (
        <div className="news-card-tags">
          {tags.map((tag) => (
            <span key={tag} className="news-tag">{tag}</span>
          ))}
        </div>
      ) : null}
      <div className="news-card-foot">
        <span>{source}</span>
        <span>{publishedAt}</span>
      </div>
    </>
  );

  if (url) {
    return (
      <a
        className="news-card news-card-link"
        href={url}
        target="_blank"
        rel="noopener noreferrer"
      >
        {body}
      </a>
    );
  }

  return <div className="news-card">{body}</div>;
}

function EmptyHint({ children }: { children: ReactNode }) {
  return <div className="empty-hint">{children}</div>;
}

function summarizePortfolio(rows: MarketRecord[]): SummarySnapshot {
  let marketValue = 0;
  let pnl = 0;
  let hasValue = false;

  for (const row of rows) {
    const quantity = readNumber(row, "quantity");
    const costPrice = readNumber(row, "cost_price");
    const currentPrice = readNumber(row, "current_price");
    const market = readNumber(row, "market_value");
    const cost = readNumber(row, "cost_value");
    const rowMarketValue = Number.isFinite(market) && market > 0 ? market : quantity && currentPrice ? quantity * currentPrice : null;
    const rowCostValue = Number.isFinite(cost) && cost > 0 ? cost : quantity && costPrice ? quantity * costPrice : null;
    const rowPnl = readNumber(row, "pnl");

    if (rowMarketValue != null) {
      marketValue += rowMarketValue;
      hasValue = true;
    }
    if (rowPnl != null) {
      pnl += rowPnl;
      hasValue = true;
    } else if (rowMarketValue != null && rowCostValue != null) {
      pnl += rowMarketValue - rowCostValue;
    }
  }

  return {
    marketValue: hasValue ? marketValue : null,
    pnl: hasValue ? pnl : null,
  };
}

function asRecord(value: unknown): MarketRecord | null {
  return isRecord(value) ? value : null;
}

function readSidebarCache(): DashboardSidebarPayload | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return isRecord(parsed) ? (parsed as DashboardSidebarPayload) : null;
  } catch {
    return null;
  }
}

function writeSidebarCache(payload: DashboardSidebarPayload): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SIDEBAR_CACHE_KEY, JSON.stringify(payload));
  } catch {
    // Ignore cache write failures and keep the live payload.
  }
}

function asRecordArray(value: unknown): MarketRecord[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord);
}

function isRecord(value: unknown): value is MarketRecord {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function readNumber(item: unknown, key: string): number {
  if (!item || typeof item !== "object") return 0;
  const value = (item as MarketRecord)[key];
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function readOptionalNumber(item: unknown, key: string): number | null {
  if (!item || typeof item !== "object") return null;
  const value = (item as MarketRecord)[key];
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringValue(item: MarketRecord, key: string): string {
  const value = item[key];
  return typeof value === "string" ? value : "";
}

function stringKey(item: MarketRecord, key: string): string {
  return stringValue(item, key) || JSON.stringify(item);
}

function formatPct(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return "--";
  if (value >= 10000) return value.toFixed(0);
  if (value >= 1000) return value.toFixed(1);
  return value.toFixed(2);
}

function formatAssetValue(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const abs = Math.abs(value);
  if (abs >= 100000000) return `${(abs / 100000000).toFixed(2)}亿`;
  if (abs >= 10000) return `${(abs / 10000).toFixed(2)}万`;
  return `${abs.toFixed(0)}元`;
}

function formatPnlValue(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 100000000) return `${sign}${(abs / 100000000).toFixed(2)}亿`;
  if (abs >= 10000) return `${sign}${(abs / 10000).toFixed(2)}万`;
  return `${sign}${abs.toFixed(0)}元`;
}

function formatQuotePrice(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return value.toFixed(2);
}

function formatQuantity(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return `${value.toFixed(0)} 股`;
}

function formatStamp(value?: string | null): string {
  if (!value) return "刚刚";

  const date = parseStampDate(value);
  if (!date) return value;

  // 后端时间统一按 UTC 处理，前端固定加 8 小时展示北京时间
  const beijingTime = new Date(date.getTime() + 8 * 60 * 60 * 1000);
  const month = String(beijingTime.getUTCMonth() + 1).padStart(2, "0");
  const day = String(beijingTime.getUTCDate()).padStart(2, "0");
  const hour = String(beijingTime.getUTCHours()).padStart(2, "0");
  const minute = String(beijingTime.getUTCMinutes()).padStart(2, "0");

  return `${month}/${day} ${hour}:${minute}`;
}

function formatNewsPublishedText(value?: string | null): string {
  if (!value) return "--";
  const trimmed = value.trim();
  if (!trimmed) return "--";

  if (/^\d{10,13}$/.test(trimmed)) {
    const numeric = Number(trimmed);
    const millis = trimmed.length === 10 ? numeric * 1000 : numeric;
    const date = new Date(millis);
    if (!Number.isNaN(date.getTime())) {
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      const hour = String(date.getHours()).padStart(2, "0");
      const minute = String(date.getMinutes()).padStart(2, "0");
      return `${month}/${day} ${hour}:${minute}`;
    }
  }

  const matched = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::\d{2})?)?$/);
  if (matched) {
    const [, , month, day, hour, minute] = matched;
    if (hour && minute) return `${month}/${day} ${hour}:${minute}`;
    return `${month}/${day}`;
  }

  return trimmed;
}

function parseStampDate(value: string): Date | null {
  const trimmed = value.trim();
  if (!trimmed) return null;

  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed);
  if (hasTimezone) {
    const zoned = new Date(trimmed);
    return Number.isNaN(zoned.getTime()) ? null : zoned;
  }

  const normalized = trimmed.includes("T") ? `${trimmed}Z` : `${trimmed.replace(" ", "T")}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function toneClass(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "neutral";
  if (value > 0) return "positive";  // 红色 = 上涨
  if (value < 0) return "negative";  // 绿色 = 下跌
  return "neutral";
}

function holdingToneClass(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "holding-surface-flat";
  if (value > 0) return "holding-surface-rise";
  if (value < 0) return "holding-surface-fall";
  return "holding-surface-flat";
}

function assetToneClass(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "asset-tone-flat";
  if (value > 0) return "asset-tone-rise";
  if (value < 0) return "asset-tone-fall";
  return "asset-tone-flat";
}
