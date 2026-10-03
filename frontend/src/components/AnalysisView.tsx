import { useEffect, useState } from "react";
import { api } from "../api";
import type { LinkDetail, Recipe } from "../types";

function RecipeView({ recipe }: { recipe: Recipe }) {
  const servings = /^\d+$/.test(recipe.servings) ? `${recipe.servings} porzioni` : recipe.servings;
  const facts = [recipe.cuisine, servings, recipe.time].filter(Boolean);
  return (
    <div className="recipe">
      {facts.length > 0 && <p className="muted">{facts.join(" · ")}</p>}
      {recipe.ingredients.length > 0 && (
        <>
          <h4>Ingredienti</h4>
          <ul>{recipe.ingredients.map((i, n) => <li key={n}>{i.text}</li>)}</ul>
        </>
      )}
      {recipe.steps.length > 0 && (
        <>
          <h4>Preparazione</h4>
          <ol>{recipe.steps.map((s, n) => <li key={n}>{s}</li>)}</ol>
        </>
      )}
      {recipe.tips.length > 0 && (
        <>
          <h4>Consigli</h4>
          <ul>{recipe.tips.map((t, n) => <li key={n}>{t}</li>)}</ul>
        </>
      )}
    </div>
  );
}

export function AnalysisToggle({ url }: { url: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>Dettagli</summary>
      {open && <AnalysisView url={url} />}
    </details>
  );
}

export function AnalysisView({ url }: { url: string }) {
  const [detail, setDetail] = useState<LinkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    api.linkDetail(url).then(setDetail).catch((e: Error) => setError(e.message));
  }, [url]);

  if (error) return <p className="muted">{error}</p>;
  if (!detail) return <p className="muted">Carico…</p>;
  if (detail.kind === "recipe" && detail.recipe) return <RecipeView recipe={detail.recipe} />;
  return (
    <div className="analysis">
      {detail.summary && <p>{detail.summary}</p>}
      {detail.key_points && detail.key_points.length > 0 && <ul>{detail.key_points.map((p, n) => <li key={n}>{p}</li>)}</ul>}
      {detail.actions && detail.actions.length > 0 && (
        <>
          <h4>Da provare</h4>
          <ul>{detail.actions.map((a, n) => <li key={n}>{a}</li>)}</ul>
        </>
      )}
    </div>
  );
}
