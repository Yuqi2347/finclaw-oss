import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  content: string;
  enableCitations?: boolean;
  variant?: "message" | "panel" | "compact";
};

const bareUrlPattern = /(?<!\]\()(?<!["'=])(https?:\/\/[^\s<>()]+[^\s<>().,;:!?])/g;
const citationPattern = /\[(\d{1,2})\](?!\()/g;

function linkifyBareUrls(content: string) {
  return content.replace(bareUrlPattern, (url) => `[${url}](${url})`);
}

function linkifyCitations(content: string, enabled?: boolean) {
  if (!enabled) return content;
  return content.replace(citationPattern, (_match, marker) => `[[${marker}]](#source-${marker})`);
}

export const MarkdownView = memo(function MarkdownView({ content, enableCitations, variant = "message" }: Props) {
  const prepared = useMemo(
    () => linkifyBareUrls(linkifyCitations(content, enableCitations)),
    [content, enableCitations],
  );
  return (
    <div className={`markdown-view markdown-view-${variant}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => (
            <div className="markdown-table-scroll">
              <table>{children}</table>
            </div>
          ),
          pre: ({ children }) => (
            <div className="markdown-code-scroll">
              <pre>{children}</pre>
            </div>
          ),
          a: ({ href, children }) => {
            if (href?.startsWith("#source-")) {
              return (
                <a
                  href={href}
                  className="inline-citation"
                  onClick={(event) => {
                    event.preventDefault();
                    const marker = Number(href.replace("#source-", ""));
                    window.dispatchEvent(new CustomEvent("finclaw:open-source", { detail: { marker } }));
                    document.querySelector(href)?.scrollIntoView({ behavior: "smooth", block: "center" });
                  }}
                >
                  {children}
                </a>
              );
            }
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
});
