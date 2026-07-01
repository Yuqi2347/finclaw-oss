import { memo, useEffect, useMemo, useState } from "react";
import type { WebSource } from "../types";

type Props = {
  sources?: WebSource[];
};

export const SourceStrip = memo(function SourceStrip({ sources }: Props) {
  const normalized = useMemo(() => dedupeSources(sources ?? []), [sources]);
  const [expanded, setExpanded] = useState(false);
  const [activeMarker, setActiveMarker] = useState<number | null>(null);

  useEffect(() => {
    function handleOpenSource(event: Event) {
      const marker = Number((event as CustomEvent<{ marker?: number }>).detail?.marker);
      if (!Number.isFinite(marker)) return;
      setExpanded(true);
      setActiveMarker(marker);
      window.setTimeout(() => {
        document.querySelector(`#source-${marker}`)?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
      }, 80);
    }
    window.addEventListener("finclaw:open-source", handleOpenSource);
    return () => window.removeEventListener("finclaw:open-source", handleOpenSource);
  }, []);

  if (!normalized.length) return null;

  return (
    <section className={`source-strip ${expanded ? "expanded" : ""}`} aria-label="联网来源">
      <button className="source-strip-header" type="button" onClick={() => setExpanded((value) => !value)}>
        <span className="source-search-icon" aria-hidden="true">⌕</span>
        <span>已阅读 {normalized.length} 个网页</span>
        <span className="source-provider-dots" aria-hidden="true">
          {normalized.slice(0, 4).map((source, index) => (
            <span key={`${source.provider}-${index}`}>{providerInitial(source.provider || source.domain || "")}</span>
          ))}
        </span>
      </button>
      {expanded ? (
        <div className="source-card-row">
          {normalized.map((source, index) => {
            const marker = source.marker ?? index + 1;
            const isActive = activeMarker === marker;
            return (
              <article className={`source-card ${isActive ? "active" : ""}`} id={`source-${marker}`} key={`${source.url}-${marker}`}>
                <button className="source-card-main" type="button" onClick={() => setActiveMarker(isActive ? null : marker)}>
                  <span className="source-marker">[{marker}]</span>
                  <span className="source-card-text">
                    <span className="source-title">{source.title || source.domain || source.url}</span>
                    <span className="source-meta">
                      <span>{source.domain || readableDomain(source.url)}</span>
                      {source.credibility ? <span>{labelCredibility(source.credibility)}</span> : null}
                    </span>
                  </span>
                </button>
                <a href={source.url} target="_blank" rel="noreferrer" className="source-open-link">
                  打开
                </a>
                {isActive && source.excerpt ? <p>{source.excerpt}</p> : null}
              </article>
            );
          })}
        </div>
      ) : null}
    </section>
  );
});

function dedupeSources(sources: WebSource[]) {
  const seen = new Set<string>();
  const result: WebSource[] = [];
  for (const source of sources) {
    if (!source?.url) continue;
    const key = source.url.split("#")[0];
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(source);
  }
  return result;
}

function readableDomain(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function labelCredibility(value: string) {
  const labels: Record<string, string> = {
    official: "官方",
    finance_media: "财经",
    major_media: "媒体",
    search_result: "搜索",
  };
  return labels[value] ?? value;
}

function providerInitial(value: string) {
  const normalized = value.toLowerCase();
  if (normalized.includes("tavily")) return "T";
  if (normalized.includes("brave")) return "B";
  if (normalized.includes("exa")) return "E";
  if (normalized.includes("serp")) return "S";
  if (normalized.includes("you")) return "Y";
  return normalized.slice(0, 1).toUpperCase() || "W";
}
