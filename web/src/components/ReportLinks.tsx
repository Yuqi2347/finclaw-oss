import { memo, useMemo } from "react";
import { collectReportLinks } from "../api/finclaw";
import type { ReportLink, ToolCallRecord } from "../types";

type Props = {
  toolCalls?: ToolCallRecord[];
  links?: ReportLink[];
};

export const ReportLinks = memo(function ReportLinks({ toolCalls = [], links: directLinks = [] }: Props) {
  const links = useMemo(
    () => [...directLinks, ...toolCalls.flatMap((call) => collectReportLinks(call.result))],
    [directLinks, toolCalls],
  );
  if (!links.length) return null;

  return (
    <div className="report-links">
      {links.map((link, index) => (
        <div className="report-pill" key={`${link.view_url}-${index}`}>
          <span>{link.title}</span>
          <a href={link.view_url} target="_blank" rel="noreferrer">打开</a>
          <a href={link.download_url} target="_blank" rel="noreferrer">下载</a>
        </div>
      ))}
    </div>
  );
});
