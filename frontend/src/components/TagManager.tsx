import { useEffect, useState } from "react";
import { api } from "../api";
import type { TagInfo } from "../types";

export function TagManager({ onChange }: { onChange: () => void }) {
  const [info, setInfo] = useState<TagInfo | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.tags().then(setInfo).catch((e: Error) => setError(e.message));
  }, []);

  const save = async (tags: string[]) => {
    try {
      const { linking } = await api.saveTags(tags);
      setInfo((current) => (current ? { ...current, linking } : current));
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!info) return error ? <div className="banner error">{error}</div> : null;
  const toggle = (tag: string) =>
    save(info.linking.includes(tag) ? info.linking.filter((t) => t !== tag) : [...info.linking, tag]);
  const suggestions = info.available.filter((a) => !info.linking.includes(a.tag));

  return (
    <div className="tags">
      <h3>Tag di collegamento</h3>
      <p className="muted">
        Solo i tag scelti qui creano collegamenti tra i documenti. Gli altri restano semplici etichette.
      </p>
      <div className="chips">
        {info.linking.length === 0 && <span className="muted">Nessun tag scelto: il grafo non ha collegamenti per tag.</span>}
        {info.linking.map((tag) => (
          <button key={tag} className="chip active" onClick={() => toggle(tag)} title="Rimuovi">#{tag} ×</button>
        ))}
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft.trim()) save([...info.linking, draft]).then(() => setDraft(""));
        }}
      >
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="nuovo tag, es. progetto-x" aria-label="Nuovo tag di collegamento" />
        <button type="submit">Aggiungi</button>
      </form>
      {suggestions.length > 0 && (
        <>
          <p className="muted">Tag esistenti (numero di documenti):</p>
          <div className="chips">
            {suggestions.slice(0, 24).map((s) => (
              <button key={s.tag} className="chip" onClick={() => toggle(s.tag)}>#{s.tag} · {s.count}</button>
            ))}
          </div>
        </>
      )}
      {error && <div className="banner error">{error}</div>}
    </div>
  );
}
