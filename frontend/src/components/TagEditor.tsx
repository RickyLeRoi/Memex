import { useState } from "react";
import { api } from "../api";
import type { DeleteKind, TagOrigin } from "../types";

const ORIGIN_HINT: Record<TagOrigin, string> = {
  model: "assegnato dal modello",
  auto: "automatico: ancora nessun tag vero",
  manual: "aggiunto da te",
};

interface Props {
  kind: DeleteKind;
  id: string;
  tags: string[];
  origin: Record<string, TagOrigin>;
  linking: string[];
  onChange: (tags: string[], origin: Record<string, TagOrigin>) => void;
}

export function TagEditor({ kind, id, tags, origin, linking, onChange }: Props) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const edit = async (add: string[], remove: string[]) => {
    setError(null);
    try {
      const result = await api.editTags(kind, id, add, remove);
      onChange(result.tags, result.tag_origin);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="tag-editor">
      <div className="chips">
        {tags.map((tag) => {
          const tagOrigin = origin[tag] ?? "model";
          return (
            <span key={tag} className={`chip ${tagOrigin} ${linking.includes(tag) ? "active" : ""}`} title={ORIGIN_HINT[tagOrigin]}>
              #{tag}
              <button
                type="button"
                className="chip-remove"
                aria-label={`Rimuovi il tag ${tag}`}
                disabled={tags.length === 1}
                onClick={() => edit([], [tag])}
              >
                ×
              </button>
            </span>
          );
        })}
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft.trim()) edit([draft], []).then(() => setDraft(""));
        }}
      >
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="aggiungi tag" aria-label="Aggiungi un tag" />
        <button type="submit" disabled={!draft.trim()}>Aggiungi</button>
      </form>
      {error && <div className="banner error" role="alert">{error}</div>}
    </div>
  );
}
