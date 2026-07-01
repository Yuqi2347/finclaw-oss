import { memo } from "react";
import { MarkdownView } from "./MarkdownView";

type MemoryDocType = "profile" | "playbook" | "convictions";

type Props = {
  type: MemoryDocType;
  content: string;
  compact?: boolean;
};

type PlaybookDimension = {
  title: string;
  questions: string[];
};

type ConvictionBlock = {
  status: string;
  title: string;
  fields: Array<{ label: string; value: string }>;
  raw: string;
};

function cleanLine(line: string) {
  return line.replace(/^\s*[-*]\s*/, "").trim();
}

function extractSection(content: string, marker: string) {
  const text = String(content || "");
  const match = new RegExp(`^##\\s+\\[${marker}\\].*$`, "m").exec(text);
  if (!match) return "";
  const start = match.index + match[0].length;
  const next = /^##\s+/m.exec(text.slice(start));
  const end = next ? start + next.index : text.length;
  return text.slice(start, end).trim();
}

function parsePlaybookDimensions(content: string): PlaybookDimension[] {
  const lines = String(content || "").split(/\r?\n/);
  const dimensions: PlaybookDimension[] = [];
  let current: PlaybookDimension | null = null;

  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith("<!--")) continue;
    if (/^维度\S*[：:]/.test(line)) {
      current = { title: line, questions: [] };
      dimensions.push(current);
      continue;
    }
    if (current) {
      current.questions.push(cleanLine(line));
    }
  }

  return dimensions.filter((item) => item.title || item.questions.length);
}

function parseConvictions(content: string): ConvictionBlock[] {
  const text = String(content || "");
  const pattern = /(?:<!-- finclaw-memory:.*?-->\s*)?###\s+\[(active|watching|stale|invalidated)\]\s+(.+?)(?=\n(?:<!-- finclaw-memory:|###\s+\[)|\s*$)/gis;
  const blocks: ConvictionBlock[] = [];

  for (const match of text.matchAll(pattern)) {
    const status = match[1];
    const title = match[2].split("\n")[0].trim();
    const raw = match[0].trim();
    const fields: Array<{ label: string; value: string }> = [];
    const fieldPattern = /^-\s+\*\*(.+?)\*\*\s*[：:]\s*(.+)$/gm;
    for (const field of raw.matchAll(fieldPattern)) {
      fields.push({ label: field[1].trim(), value: field[2].trim() });
    }
    blocks.push({ status, title, fields, raw });
  }

  return blocks;
}

function ProfileView({ content }: { content: string }) {
  const level = extractSection(content, "LEVEL");
  const snapshot = extractSection(content, "SNAPSHOT");
  const log = extractSection(content, "LOG");

  if (!level && !snapshot && !log) {
    return <MarkdownView content={content || "暂无内容"} variant="panel" />;
  }

  return (
    <div className="memory-visual memory-visual-profile">
      {level && (
        <section className="memory-visual-block">
          <div className="memory-visual-eyebrow">当前阶段</div>
          <MarkdownView content={level} variant="compact" />
        </section>
      )}
      {snapshot && (
        <section className="memory-visual-block featured">
          <div className="memory-visual-eyebrow">人物志快照</div>
          <MarkdownView content={snapshot} variant="panel" />
        </section>
      )}
      {log && (
        <section className="memory-visual-block">
          <div className="memory-visual-eyebrow">评估窗口 LOG</div>
          <MarkdownView content={log} variant="compact" />
        </section>
      )}
    </div>
  );
}

function PlaybookView({ content }: { content: string }) {
  const dimensions = parsePlaybookDimensions(content);
  if (!dimensions.length) {
    return <MarkdownView content={content || "暂无内容"} variant="panel" />;
  }

  return (
    <div className="memory-visual memory-visual-playbook">
      {dimensions.map((dimension) => (
        <section key={dimension.title} className="playbook-dimension-card">
          <h4>{dimension.title}</h4>
          {dimension.questions.length > 0 && (
            <ul>
              {dimension.questions.map((question, index) => (
                <li key={`${dimension.title}-${index}`}>{question}</li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  );
}

function ConvictionsView({ content }: { content: string }) {
  const blocks = parseConvictions(content);
  if (!blocks.length) {
    return <MarkdownView content={content || "暂无内容"} variant="panel" />;
  }

  return (
    <div className="memory-visual memory-visual-convictions">
      {blocks.map((block) => (
        <section key={`${block.status}-${block.title}`} className={`conviction-card conviction-card-${block.status}`}>
          <div className="conviction-card-head">
            <span>{block.status}</span>
            <h4>{block.title}</h4>
          </div>
          {block.fields.length > 0 ? (
            <dl>
              {block.fields.map((field) => (
                <div key={`${block.title}-${field.label}`}>
                  <dt>{field.label}</dt>
                  <dd>{field.value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <MarkdownView content={block.raw} variant="compact" />
          )}
        </section>
      ))}
    </div>
  );
}

export const MemoryDocumentView = memo(function MemoryDocumentView({ type, content, compact }: Props) {
  if (!String(content || "").trim()) {
    return <div className="memory-visual-empty">暂无内容</div>;
  }
  if (compact) {
    return <MarkdownView content={content} variant="compact" />;
  }
  if (type === "profile") return <ProfileView content={content} />;
  if (type === "playbook") return <PlaybookView content={content} />;
  return <ConvictionsView content={content} />;
});
