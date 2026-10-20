import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AnalysisToggle } from "../components/AnalysisView";
import { DeleteDialog } from "../components/DeleteDialog";
import { JobPanel } from "../components/JobPanel";
import { ScreenshotDrop } from "../components/ScreenshotDrop";
import { TagEditor } from "../components/TagEditor";
import { formatDate, parseLinks } from "../format";
import type { DeleteTarget, LinkRow } from "../types";
import { useJob } from "../useJob";

const STATUS_LABEL = { pending: "in coda", done: "ok", error: "errore" } as const;
const SCREENSHOT_PREFIX = "image://";
const DOCUMENT_PREFIX = "file://";
const isScreenshot = (url: string) => url.startsWith(SCREENSHOT_PREFIX);
const isDocument = (url: string) => url.startsWith(DOCUMENT_PREFIX);
const hrefOf = (url: string) => {
  if (isScreenshot(url)) return `/api/media/${url.slice(SCREENSHOT_PREFIX.length)}`;
  if (isDocument(url)) return `/api/docs/${url.slice(DOCUMENT_PREFIX.length)}`;
  return url;
};
const fallbackLabel = (url: string) => (isScreenshot(url) ? "Screenshot" : isDocument(url) ? "Documento PDF" : url);

export function Links() {
  const [text, setText] = useState("");
  const [rows, setRows] = useState<LinkRow[]>([]);
  const [toDelete, setToDelete] = useState<DeleteTarget | null>(null);
  const [alreadyIngested, setAlreadyIngested] = useState(0);
  const loadRows = useCallback(() => {
    api.links().then(setRows).catch(() => undefined);
  }, []);
  const { job, error, start, cancel, busy } = useJob(loadRows);
  useEffect(loadRows, [loadRows]);
  // 20261004 ++ RG #links_file_queue links still waiting in links.txt are offered in the textarea (never over typed text)
  useEffect(() => {
    api.queuedLinksText().then(({ text: queued }) => setText((current) => current || queued)).catch(() => undefined);
  }, []);

  const parsed = useMemo(() => parseLinks(text), [text]);
  const valid = parsed.filter((p) => p.valid);
  const hasFailed = rows.some((r) => r.status === "error");

  const ingest = () =>
    start(async () => {
      const result = await api.ingestLinks(valid.map(({ url, note }) => ({ url, note })));
      setText("");
      setAlreadyIngested(result.already_ingested);
      loadRows();
      return result;
    });

  const retry = () => start(async () => {
    await api.retryLinks();
    return api.ingestFamily("links", ["links"]);
  });

  return (
    <div className="stack">
      <section className="card">
        <h2>Link esterni</h2>
        <label htmlFor="links-input" className="muted">
          Un link per riga. Dopo l'URL puoi scrivere un titolo o una breve descrizione: aiuta il modello a capire perché lo hai salvato.
        </label>
        <textarea
          id="links-input"
          rows={7}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"https://blog.esempio.it/articolo  guida per il refactoring\nhttps://www.instagram.com/reel/ABC123/  setup scrivania"}
        />
        {parsed.length > 0 && (
          <ul className="preview">
            {parsed.map((p, i) => (
              <li key={i} className={p.valid ? "" : "invalid"}>
                {p.valid ? "✓" : "✗"} <span className="mono">{p.url}</span>
                {p.note && <span className="muted"> — {p.note}</span>}
                {!p.valid && <span> (URL non valido, deve iniziare con http:// o https://)</span>}
              </li>
            ))}
          </ul>
        )}
        {alreadyIngested > 0 && (
          <div className="banner" role="status">
            {alreadyIngested} {alreadyIngested === 1 ? "link era già ingerito" : "link erano già ingeriti"}: non vengono rielaborati e sono stati tolti da links.txt.
          </div>
        )}
        <div className="row">
          <button className="primary" disabled={valid.length === 0 || busy} onClick={ingest}>
            {busy ? "Ingest in corso…" : `Ingerisci ${valid.length || ""} link`.trim()}
          </button>
          {hasFailed && <button disabled={busy} onClick={retry}>Riprova i falliti</button>}
        </div>
      </section>

      <ScreenshotDrop busy={busy} onStart={start} />

      <JobPanel job={job} error={error} onCancel={cancel} />

      <section className="card">
        <h2>Coda e storico</h2>
        {rows.length === 0 ? <p className="muted">Nessun link ancora.</p> : (
          <table>
            <thead><tr><th>Stato</th><th>Titolo / URL</th><th>Nota</th><th>Aggiunto</th><th></th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.url}>
                  <td><span className={`badge ${r.status === "error" ? "failed" : r.status}`}>{STATUS_LABEL[r.status]}</span></td>
                  <td>
                    {r.image && <img className="thumb" src={`/api/media/${r.image}`} alt="" loading="lazy" />}
                    <a href={hrefOf(r.url)} target="_blank" rel="noreferrer noopener">{r.title || r.note || fallbackLabel(r.url)}</a>
                    {r.error && <div className="muted">{r.error}</div>}
                    {r.status === "done" && <AnalysisToggle url={r.url} />}
                    {r.status === "done" && (
                      <TagEditor
                        kind="link"
                        id={r.url}
                        tags={r.tags}
                        origin={r.tag_origin}
                        linking={[]}
                        onChange={(tags, tag_origin) =>
                          setRows((current) => current.map((x) => (x.url === r.url ? { ...x, tags, tag_origin } : x)))
                        }
                      />
                    )}
                  </td>
                  <td>{r.note}</td>
                  <td>{formatDate(r.added_at)}</td>
                  <td>
                    {r.status !== "pending" && (
                      <button
                        className="link-action"
                        disabled={busy}
                        aria-label={`Rielabora ${r.title || r.url}`}
                        onClick={() => start(async () => {
                          await api.reprocessLink(r.url);
                          loadRows();
                          return api.ingestLinks([]);
                        })}
                      >
                        Rielabora
                      </button>
                    )}
                    <button
                      className="link-danger"
                      disabled={busy}
                      aria-label={`Elimina ${r.title || r.url}`}
                      onClick={() => setToDelete({ kind: "link", id: r.url, label: r.title || r.url })}
                    >
                      Elimina
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {toDelete && (
        <DeleteDialog target={toDelete} onClose={() => setToDelete(null)} onDeleted={loadRows} />
      )}
    </div>
  );
}
