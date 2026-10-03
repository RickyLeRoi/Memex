import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { JobPanel } from "../components/JobPanel";
import { formatDate } from "../format";
import type { Family, SourceStatus } from "../types";
import { useJob } from "../useJob";

const COPY: Record<Family, { title: string; hint: string }> = {
  chat: { title: "Chat", hint: "Teams e Slack: legge i messaggi nuovi dall'ultimo import." },
  mail: { title: "Email", hint: "Outlook e Gmail: legge le cartelle configurate, escludendo i mittenti automatici." },
  tickets: { title: "Ticket", hint: "Jira e canali Slack dedicati ai ticket." },
};

export function SourceFamily({ family }: { family: Family }) {
  const [sources, setSources] = useState<SourceStatus[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const load = useCallback(() => {
    api.sources().then((all) => {
      const mine = all.filter((s) => s.family === family);
      setSources(mine);
      setSelected((current) => (current.length ? current : mine.filter((s) => s.enabled && s.configured).map((s) => s.name)));
    }).catch(() => undefined);
  }, [family]);
  const { job, error, start, busy } = useJob(load);
  useEffect(load, [load]);

  const toggle = (name: string) =>
    setSelected((current) => (current.includes(name) ? current.filter((n) => n !== name) : [...current, name]));

  return (
    <div className="stack">
      <section className="card">
        <h2>{COPY[family].title}</h2>
        <p className="muted">{COPY[family].hint}</p>
        <table>
          <thead><tr><th></th><th>Sorgente</th><th>Stato</th><th>Documenti</th><th>Ultimo import</th></tr></thead>
          <tbody>
            {sources.map((s) => {
              const ready = s.enabled && s.configured;
              return (
                <tr key={s.name}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Includi ${s.label}`}
                      disabled={!ready}
                      checked={selected.includes(s.name) && ready}
                      onChange={() => toggle(s.name)}
                    />
                  </td>
                  <td>{s.label}</td>
                  <td>
                    {ready && <span className="badge done">pronta</span>}
                    {!s.configured && <span className="badge failed">non configurata</span>}
                    {s.configured && !s.enabled && <span className="badge off">disattivata</span>}
                  </td>
                  <td>{s.documents}</td>
                  <td>{formatDate(s.last_import)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {sources.some((s) => !s.configured || !s.enabled) && (
          <p className="muted">Per attivare una sorgente imposta la sua sezione in <span className="mono">config.toml</span> (vedi README).</p>
        )}
        <div className="row">
          <button
            className="primary"
            disabled={busy || selected.length === 0}
            onClick={() => start(() => api.ingestFamily(family, selected))}
          >
            {busy ? "Ingest in corso…" : "Ingerisci"}
          </button>
        </div>
      </section>
      <JobPanel job={job} error={error} />
    </div>
  );
}
