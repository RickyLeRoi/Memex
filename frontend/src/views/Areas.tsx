import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { JobPanel } from "../components/JobPanel";
import type { Area } from "../types";
import { useJob } from "../useJob";

const NEW_COLOR = "#6366f1";

function Proposal({ area, busy, onApprove, onReject }: {
  area: Area;
  busy: boolean;
  onApprove: (changes: { label: string; description: string; color: string }) => void;
  onReject: () => void;
}) {
  const [label, setLabel] = useState(area.label);
  const [description, setDescription] = useState(area.description || area.why);
  const [color, setColor] = useState(area.color || NEW_COLOR);
  return (
    <li className="card proposal">
      <div className="row">
        <input type="color" value={color} onChange={(e) => setColor(e.target.value)} aria-label="Colore" />
        <input value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Nome dell'area" />
        <span className="muted">proposta {area.proposals} {area.proposals === 1 ? "volta" : "volte"}</span>
      </div>
      <input
        className="wide"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        aria-label="Descrizione per il modello"
        placeholder="descrizione: aiuta il modello a capire cosa ci rientra"
      />
      {area.evidence.length > 0 && (
        <p className="muted">Proposta da: {area.evidence.join(" · ")}</p>
      )}
      <div className="row">
        <button className="primary" disabled={busy || !label.trim()} onClick={() => onApprove({ label, description, color })}>
          Approva e rivedi l'archivio
        </button>
        <button disabled={busy} onClick={onReject}>Rifiuta</button>
      </div>
    </li>
  );
}

export function Areas() {
  const [areas, setAreas] = useState<Area[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState({ label: "", description: "", color: NEW_COLOR });
  const load = useCallback(() => {
    api.areas().then(setAreas).catch((e: Error) => setError(e.message));
  }, []);
  const { job, error: jobError, start, busy } = useJob(load);
  useEffect(load, [load]);

  const run = async (action: () => Promise<unknown>, message?: string) => {
    setError(null);
    setNotice(null);
    try {
      await action();
      if (message) setNotice(message);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const proposed = areas.filter((a) => a.status === "proposed");
  const active = areas.filter((a) => a.status === "active");
  const rejected = areas.filter((a) => a.status === "rejected");

  return (
    <div className="stack">
      {error && <div className="banner error" role="alert">{error}</div>}
      {notice && <div className="banner" role="status">{notice}</div>}

      <section className="card">
        <h2>Proposte del modello</h2>
        {proposed.length === 0 ? (
          <p className="muted">Nessuna proposta. Quando nessuna area calza, il modello può suggerirne una nuova: la vedi qui e decidi tu.</p>
        ) : (
          <ul className="plain">
            {proposed.map((area) => (
              <Proposal
                key={area.name}
                area={area}
                busy={busy}
                onApprove={(changes) => start(() => api.approveArea(area.name, changes))}
                onReject={() => run(() => api.rejectArea(area.name), `Proposta «${area.label}» rifiutata: non verrà riproposta.`)}
              />
            ))}
          </ul>
        )}
        <p className="muted">
          Approvando, il modello rivede tutto l'archivio (solo i dati già salvati, senza riscaricare) e sposta nella nuova area
          ciò che ci rientra. Le aree scelte a mano non vengono mai toccate.
        </p>
      </section>

      <JobPanel job={job} error={jobError} />

      <section className="card">
        <h2>Aree</h2>
        <table>
          <thead>
            <tr><th></th><th>Area</th><th>Descrizione per il modello</th><th>Voci</th><th>Fuori dal vault</th><th></th></tr>
          </thead>
          <tbody>
            {active.map((area) => (
              <AreaRow key={area.name} area={area} onSave={(changes) => run(() => api.updateArea(area.name, changes))}
                onDelete={() => {
                  const message = area.count > 0
                    ? `Eliminare «${area.label}»? Le sue ${area.count} voci passano ad «Altro».`
                    : `Eliminare «${area.label}»?`;
                  if (window.confirm(message)) run(() => api.deleteArea(area.name), `Area «${area.label}» eliminata.`);
                }}
                onCleanVault={() => {
                  if (window.confirm(`Togliere dal vault Obsidian ciò che è già nell'area «${area.label}»? Il database e i report restano.`)) {
                    run(() => api.cleanVault(area.name).then((r) => {
                      if (r.errors.length) throw new Error(r.errors.join("; "));
                      setNotice(`Vault ripulito: ${r.edited.length} file toccati.`);
                    }));
                  }
                }}
              />
            ))}
          </tbody>
        </table>
        <p className="muted">
          «Fuori dal vault»: le voci di quell'area non vengono scritte in Obsidian (né note, né righe, né immagini). Non è attivo
          per nessuna area finché non lo scegli. Il database e i report non cambiano.
        </p>
      </section>

      <section className="card">
        <h2>Nuova area</h2>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.createArea(draft.label, draft.description, draft.color).then(() =>
              setDraft({ label: "", description: "", color: NEW_COLOR })), `Area «${draft.label}» creata.`);
          }}
        >
          <input type="color" value={draft.color} onChange={(e) => setDraft({ ...draft, color: e.target.value })} aria-label="Colore" />
          <input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} placeholder="nome" aria-label="Nome della nuova area" />
          <input className="wide" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="descrizione per il modello" aria-label="Descrizione della nuova area" />
          <button type="submit" disabled={!draft.label.trim()}>Aggiungi</button>
        </form>
        <p className="muted">Un'area aggiunta da te è attiva subito. Per rivedere ciò che c'è già, assegna a mano o usa «Rielabora» sui link.</p>
      </section>

      {rejected.length > 0 && (
        <section className="card">
          <h2>Proposte rifiutate</h2>
          <p className="muted">Il modello non le riproporrà.</p>
          <ul className="plain">
            {rejected.map((area) => (
              <li key={area.name} className="row">
                <span>{area.label}</span>
                <button onClick={() => run(() => api.createArea(area.label, area.description, area.color || NEW_COLOR), `Area «${area.label}» creata.`)}>
                  Crea comunque
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function AreaRow({ area, onSave, onDelete, onCleanVault }: {
  area: Area;
  onSave: (changes: Partial<Pick<Area, "label" | "description" | "color" | "exclude_from_vault">>) => void;
  onDelete: () => void;
  onCleanVault: () => void;
}) {
  const [label, setLabel] = useState(area.label);
  const [description, setDescription] = useState(area.description);
  const dirty = label !== area.label || description !== area.description;
  return (
    <tr>
      <td><input type="color" value={area.color} onChange={(e) => onSave({ color: e.target.value })} aria-label={`Colore di ${area.label}`} /></td>
      <td><input value={label} onChange={(e) => setLabel(e.target.value)} aria-label={`Nome di ${area.label}`} /></td>
      <td>
        <input className="wide" value={description} onChange={(e) => setDescription(e.target.value)} aria-label={`Descrizione di ${area.label}`} />
        {dirty && <button onClick={() => onSave({ label, description })}>Salva</button>}
      </td>
      <td>{area.count}</td>
      <td>
        {area.system ? <span className="muted">—</span> : (
          <>
            <input type="checkbox" checked={area.exclude_from_vault} aria-label={`Tieni ${area.label} fuori dal vault`}
              onChange={(e) => onSave({ exclude_from_vault: e.target.checked })} />
            {area.exclude_from_vault && <button className="link-action" onClick={onCleanVault}>Ripulisci il vault</button>}
          </>
        )}
      </td>
      <td>{!area.system && <button className="link-danger" onClick={onDelete}>Elimina</button>}</td>
    </tr>
  );
}
