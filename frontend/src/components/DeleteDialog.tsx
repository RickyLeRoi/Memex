import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DeleteResult, DeleteTarget, Impact } from "../types";

interface Props {
  target: DeleteTarget;
  onClose: () => void;
  onDeleted: () => void;
}

const ACTION_LABEL = { edit: "modificato", delete: "eliminato" } as const;

export function DeleteDialog({ target, onClose, onDeleted }: Props) {
  const [impact, setImpact] = useState<Impact | null>(null);
  const [result, setResult] = useState<DeleteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    api.deleteImpact(target.kind, target.id).then(setImpact).catch((e: Error) => setError(e.message));
  }, [target.kind, target.id]);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !working && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, working]);

  const confirm = async () => {
    if (!impact) return;
    setWorking(true);
    setError(null);
    try {
      setResult(await api.deleteDocument(impact.kind, impact.id, impact.token));
      onDeleted();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorking(false);
    }
  };

  const blocking = result?.leftovers.filter((l) => l.blocking) ?? [];
  const mentions = result?.leftovers.filter((l) => !l.blocking) ?? [];

  return (
    <div className="overlay" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && !working && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">
        <h2 id="delete-title">{result ? "Eliminazione completata" : "Eliminare questa informazione?"}</h2>
        <p><strong>{target.label}</strong></p>

        {!result && impact && (
          <>
            <p className="muted">
              Sparirà ovunque esista e non si può annullare: {impact.db.links} link e {impact.db.items} elementi nel database
              {impact.files.length > 0 && `, più ${impact.files.length} file qui sotto`}.
            </p>
            {impact.files.length > 0 && (
              <ul className="impact">
                {impact.files.map((f) => (
                  <li key={f.path}>
                    <span className={`badge ${f.action === "delete" ? "failed" : "off"}`}>{ACTION_LABEL[f.action]}</span>{" "}
                    <span className="mono">{f.path}</span> <span className="muted">— {f.detail}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="muted">
              Nei report toccati vengono rimossi anche gli "In evidenza", perché sono testo libero e non si può sapere
              quali righe riguardino questa informazione.
            </p>
          </>
        )}
        {!result && !impact && !error && <p className="muted">Calcolo cosa verrebbe eliminato…</p>}

        {result && (
          <div aria-live="polite">
            {result.ok ? (
              <p>Fatto. La verifica finale non ha trovato tracce residue.</p>
            ) : (
              <div className="banner error" role="alert">
                {result.errors.map((e) => <div key={e}>{e}</div>)}
                {blocking.map((l) => <div key={l.path}>Traccia residua: <span className="mono">{l.path}</span></div>)}
                <div>Riprova l'eliminazione: è idempotente.</div>
              </div>
            )}
            {mentions.length > 0 && (
              <>
                <p className="muted">Menzioni dell'URL in altri contenuti, non eliminate:</p>
                <ul className="impact">{mentions.map((m) => <li key={m.path} className="mono">{m.path}</li>)}</ul>
              </>
            )}
          </div>
        )}
        {error && <div className="banner error" role="alert">{error}</div>}

        <div className="row">
          {!result && (
            <button className="danger" disabled={!impact || working} onClick={confirm}>
              {working ? "Elimino…" : "Elimina definitivamente"}
            </button>
          )}
          <button ref={cancelRef} disabled={working} onClick={onClose}>{result ? "Chiudi" : "Annulla"}</button>
        </div>
      </div>
    </div>
  );
}
