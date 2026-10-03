import { useEffect, useRef } from "react";
import type { JobState } from "../types";

const LABEL = { running: "In corso…", done: "Completato", failed: "Fallito", cancelled: "Interrotto" } as const;

export function JobPanel({ job, error, onCancel }: {
  job: JobState | null;
  error: string | null;
  onCancel?: () => void;
}) {
  const logRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [job?.log.length]);

  if (error) return <div className="banner error" role="alert">{error}</div>;
  if (!job) return null;
  return (
    <section className="card" aria-live="polite">
      <header className="row">
        <span className={`badge ${job.status}`}>{LABEL[job.status]}</span>
        {job.status === "running" && onCancel && <button onClick={onCancel}>Interrompi</button>}
        {job.status === "failed" && <span className="muted">exit code {job.exit_code}</span>}
      </header>
      <pre className="log" ref={logRef}>{job.log.join("\n") || "In attesa dell'output…"}</pre>
    </section>
  );
}
