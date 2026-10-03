import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AnalysisView } from "../components/AnalysisView";
import { BarChart } from "../components/BarChart";
import { DeleteDialog } from "../components/DeleteDialog";
import { Graph, sourceColor } from "../components/Graph";
import { TagEditor } from "../components/TagEditor";
import { TagManager } from "../components/TagManager";
import { formatDate } from "../format";
import type { Area, DeleteTarget, GraphData, GraphNode, SourceStatus, Stats } from "../types";

const NO_AREA_COLOR = "#9ca3af";

function deleteTargetOf(node: GraphNode): DeleteTarget {
  if (node.type === "item") return { kind: "item", id: node.id.replace(/^item:/, ""), label: node.label };
  return { kind: node.source === "links" ? "link" : "doc", id: node.id, label: node.label };
}

export function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [sources, setSources] = useState<SourceStatus[]>([]);
  const [areas, setAreas] = useState<Area[]>([]);
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [includeDomain, setIncludeDomain] = useState(false);
  const [colorBy, setColorBy] = useState<"area" | "source">("area");
  const [islands, setIslands] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<DeleteTarget | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(() => window.matchMedia("(min-width: 721px)").matches);

  const loadGraph = useCallback(() => {
    api.graph(includeDomain).then(setGraph).catch((e: Error) => setError(e.message));
  }, [includeDomain]);

  const loadStats = useCallback(() => {
    Promise.all([api.stats(), api.sources(), api.areas()])
      .then(([s, src, a]) => {
        setStats(s);
        setSources(src);
        setAreas(a);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(loadStats, [loadStats]);
  useEffect(loadGraph, [loadGraph]);

  const activeAreas = useMemo(() => areas.filter((a) => a.status === "active"), [areas]);
  const areaByName = useMemo(() => new Map(areas.map((a) => [a.name, a])), [areas]);
  const graphSources = useMemo(() => [...new Set(graph?.nodes.map((n) => n.source) ?? [])].sort(), [graph]);

  // Areas only filter and colour: hiding one never changes any edge.
  const visibleGraph = useMemo<GraphData | null>(() => {
    if (!graph) return null;
    if (hidden.size === 0) return graph;
    const nodes = graph.nodes.filter((n) => !hidden.has(n.area));
    const ids = new Set(nodes.map((n) => n.id));
    return { ...graph, nodes, edges: graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
  }, [graph, hidden]);

  const colorOf = useCallback(
    (node: GraphNode) =>
      colorBy === "area" ? areaByName.get(node.area)?.color ?? NO_AREA_COLOR : sourceColor(node.source, graphSources),
    [colorBy, areaByName, graphSources],
  );

  const afterDelete = () => {
    setSelected(null);
    loadStats();
    loadGraph();
  };

  const toggleArea = (name: string) =>
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  if (error) return <div className="container"><div className="banner error" role="alert">{error}</div></div>;
  if (!stats) return <p className="muted container">Caricamento…</p>;

  const edgeCount = (type: string) => visibleGraph?.edges.filter((e) => e.type === type).length ?? 0;
  const shownSelected = selected && !hidden.has(selected.area) ? selected : null;

  return (
    <>
      <section className="hero" aria-label="Grafo dei documenti" data-drawer={shownSelected ? "open" : "closed"}>
        {visibleGraph && visibleGraph.nodes.length > 0 ? (
          <Graph data={visibleGraph} selectedId={shownSelected?.id ?? null} onSelect={setSelected}
            colorOf={colorOf} islands={islands} />
        ) : (
          <p className="muted hero-empty">Nessun documento da mostrare: importa qualcosa dalle altre sezioni.</p>
        )}

        <details className="toolbar" open={filtersOpen} onToggle={(e) => setFiltersOpen(e.currentTarget.open)}>
          <summary>
            Filtri <span className="muted">· {visibleGraph?.nodes.length ?? 0} nodi</span>
          </summary>
          <div className="toolbar-body">
            <div className="chips">
              {activeAreas.map((area) => (
                <button
                  key={area.name}
                  className={`chip area-chip ${hidden.has(area.name) ? "off" : ""}`}
                  aria-pressed={!hidden.has(area.name)}
                  title={hidden.has(area.name) ? "Mostra nel grafo" : "Nascondi dal grafo"}
                  onClick={() => toggleArea(area.name)}
                >
                  <i style={{ background: area.color }} /> {area.label} · {stats.by_area[area.name] ?? 0}
                </button>
              ))}
            </div>
            <label className="check">
              <input type="checkbox" checked={includeDomain} onChange={(e) => setIncludeDomain(e.target.checked)} />
              Collega documenti dello stesso dominio
            </label>
            <label className="check">
              <input type="checkbox" checked={islands} onChange={(e) => setIslands(e.target.checked)} />
              Isole per area
            </label>
            <label className="check">
              Colora per
              <select value={colorBy} onChange={(e) => setColorBy(e.target.value as "area" | "source")} aria-label="Colora i nodi per">
                <option value="area">area</option>
                <option value="source">sorgente</option>
              </select>
            </label>
            {colorBy === "source" && (
              <div className="legend">
                {graphSources.map((s) => (
                  <span key={s}><i style={{ background: sourceColor(s, graphSources) }} /> {s}</span>
                ))}
              </div>
            )}
            <p className="muted small">
              {edgeCount("tag")} per tag · {edgeCount("domain")} per dominio · {edgeCount("ref")} per riferimento
            </p>
            <p className="muted small hint">Clicca un'area per mostrarla o nasconderla. Clicca un nodo per i dettagli, trascina per spostare, rotella o pizzica per zoomare.</p>
          </div>
        </details>

        <aside className="detail" inert={!shownSelected} aria-label="Dettagli nodo">
          {shownSelected && (
            <>
              <button className="detail-close" onClick={() => setSelected(null)} aria-label="Chiudi dettagli">×</button>
              {shownSelected.image && <img className="cover" src={`/api/media/${shownSelected.image}`} alt="" />}
              <h3>{shownSelected.label}</h3>
              <p className="muted">
                {shownSelected.type === "doc" ? "Documento" : `Elemento: ${shownSelected.kind}`} · {shownSelected.source}
              </p>
              {/^https?:/.test(shownSelected.url) && (
                <a href={shownSelected.url} target="_blank" rel="noreferrer noopener">{shownSelected.url}</a>
              )}
              <AreaPicker
                node={shownSelected}
                areas={activeAreas}
                onChange={(area) => {
                  setSelected({ ...shownSelected, area, area_origin: "manual" });
                  loadGraph();
                  loadStats();
                }}
              />
              {shownSelected.type === "doc" && shownSelected.source === "links" && <AnalysisView url={shownSelected.id} />}
              <div className="row">
                <button className="danger" onClick={() => setToDelete(deleteTargetOf(shownSelected))}>Elimina…</button>
              </div>
              <TagEditor
                key={shownSelected.id}
                kind={deleteTargetOf(shownSelected).kind}
                id={deleteTargetOf(shownSelected).id}
                tags={shownSelected.tags}
                origin={shownSelected.tag_origin}
                linking={graph?.linking_tags ?? []}
                onChange={(tags, tag_origin) => {
                  setSelected({ ...shownSelected, tags, tag_origin });
                  loadGraph();
                  loadStats();
                }}
              />
            </>
          )}
        </aside>
      </section>

      <div className="container stack">
        <div className="tiles">
          <Tile label="Documenti importati" value={stats.totals.documents} />
          <Tile label="Elementi estratti" value={stats.totals.items} />
          <Tile label="Link in coda" value={stats.totals.links_pending} />
          <Tile label="Ultimo import" value={formatDate(stats.last_import)} small />
        </div>

        <section className="card">
          <h2>Import negli ultimi 30 giorni</h2>
          <BarChart days={stats.per_day} />
        </section>

        <section className="card">
          <h2>Sorgenti</h2>
          <table className="stacked">
            <thead><tr><th>Sorgente</th><th>Stato</th><th>Documenti</th><th>Ultimo import</th></tr></thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.name}>
                  <td data-label="Sorgente">{s.label}</td>
                  <td data-label="Stato">
                    <span className={`badge ${s.enabled ? "done" : "off"}`}>{s.enabled ? "attiva" : "disattivata"}</span>{" "}
                    {!s.configured && <span className="badge failed">non configurata</span>}
                  </td>
                  <td data-label="Documenti">{s.documents}</td>
                  <td data-label="Ultimo import">{formatDate(s.last_import)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <TagManager onChange={loadGraph} />
        </section>
      </div>
      {toDelete && <DeleteDialog target={toDelete} onClose={() => setToDelete(null)} onDeleted={afterDelete} />}
    </>
  );
}

function AreaPicker({ node, areas, onChange }: { node: GraphNode; areas: Area[]; onChange: (area: string) => void }) {
  const [error, setError] = useState<string | null>(null);
  const target = deleteTargetOf(node);
  const change = async (area: string) => {
    setError(null);
    try {
      await api.assignArea(target.kind, target.id, area);
      onChange(area);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="row">
      <label>
        Area{" "}
        <select value={node.area} onChange={(e) => change(e.target.value)} aria-label="Area">
          {areas.map((a) => <option key={a.name} value={a.name}>{a.label}</option>)}
        </select>
      </label>
      {node.area_origin === "manual" && <span className="muted">scelta da te</span>}
      {error && <span className="muted" role="alert">{error}</span>}
    </div>
  );
}

function Tile({ label, value, small }: { label: string; value: number | string; small?: boolean }) {
  return (
    <div className="tile">
      <span className="muted">{label}</span>
      <strong className={small ? "small" : ""}>{value}</strong>
    </div>
  );
}
