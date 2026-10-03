export type Family = "chat" | "mail" | "tickets";

export interface DayStat {
  date: string;
  counts: Record<string, number>;
  total: number;
}

export interface Stats {
  totals: { documents: number; items: number; links_pending: number };
  by_source: Record<string, number>;
  by_area: Record<string, number>;
  item_kinds: Record<string, number>;
  last_import: string | null;
  last_run: string | null;
  per_day: DayStat[];
}

export interface SourceStatus {
  name: string;
  label: string;
  family: "links" | Family;
  enabled: boolean;
  configured: boolean;
  documents: number;
  last_import: string | null;
}

export type TagOrigin = "model" | "auto" | "manual";

export interface Area {
  name: string;
  label: string;
  description: string;
  color: string;
  status: "active" | "proposed" | "rejected";
  exclude_from_vault: boolean;
  system: boolean;
  proposals: number;
  why: string;
  evidence: string[];
  count: number;
}

export interface Recipe {
  title: string;
  cuisine: string;
  servings: string;
  time: string;
  ingredients: { text: string; item: string }[];
  steps: string[];
  tips: string[];
}

export interface LinkDetail {
  kind: "link" | "recipe";
  title: string | null;
  platform: string | null;
  note: string | null;
  summary: string | null;
  key_points: string[] | null;
  actions: string[] | null;
  worth_it: string | null;
  recipe: Recipe | null;
}

export interface LinkRow {
  image: string | null;
  tags: string[];
  tag_origin: Record<string, TagOrigin>;
  url: string;
  note: string;
  added_at: string;
  status: "pending" | "done" | "error";
  attempts: number;
  processed_at: string | null;
  error: string;
  title: string;
}

export interface GraphNode {
  id: string;
  type: "doc" | "item";
  source: string;
  label: string;
  url: string;
  tags: string[];
  tag_origin: Record<string, TagOrigin>;
  area: string;
  area_origin: TagOrigin;
  image?: string | null;
  kind?: string;
  timestamp?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: "ref" | "tag" | "domain";
  tags?: string[];
  domain?: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  linking_tags: string[];
}

export interface TagInfo {
  linking: string[];
  available: { tag: string; count: number }[];
}

export type DeleteKind = "link" | "doc" | "item";

export interface DeleteTarget {
  kind: DeleteKind;
  id: string;
  label: string;
}

export interface Impact {
  kind: DeleteKind;
  id: string;
  label: string;
  db: { links: number; items: number };
  files: { path: string; action: "edit" | "delete"; detail: string }[];
  token: string;
}

export interface DeleteResult {
  ok: boolean;
  db: { links: number; items: number };
  edited: string[];
  errors: string[];
  leftovers: { path: string; blocking: boolean }[];
}

export interface JobState {
  id: string;
  sources: string[];
  status: "running" | "done" | "failed";
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  log: string[];
}
