import { useEffect, useState } from "react";

const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

type Props = {
  active: boolean;
  status: string;
};

export function AgentStatus({ active, status }: Props) {
  const [frame, setFrame] = useState(0);
  const [startedAt] = useState(() => Date.now());
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => {
      setFrame((prev) => (prev + 1) % frames.length);
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 120);
    return () => window.clearInterval(id);
  }, [active, startedAt]);

  if (!active && !status) return null;

  return (
    <div className="agent-status">
      <span className="agent-spinner">{active ? frames[frame] : "✓"}</span>
      <span>{status || "Done"}</span>
      <span className="agent-elapsed">({elapsed}s)</span>
    </div>
  );
}
