import type {
  Area, DeleteKind, DeleteResult, GraphData, Impact, JobState, LinkDetail, LinkRow, SourceStatus, Stats, TagInfo, TagOrigin,
} from "./types";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error((data as { error?: string }).error ?? `HTTP ${response.status}`);
  return data as T;
}

export const api = {
  stats: () => request<Stats>("GET", "/api/stats"),
  sources: () => request<SourceStatus[]>("GET", "/api/sources"),
  links: () => request<LinkRow[]>("GET", "/api/links"),
  graph: (includeDomain: boolean) => request<GraphData>("GET", `/api/graph?domain=${includeDomain ? 1 : 0}`),
  tags: () => request<TagInfo>("GET", "/api/graph/tags"),
  saveTags: (tags: string[]) => request<{ linking: string[] }>("PUT", "/api/graph/tags", { tags }),
  retryLinks: () => request<{ ok: boolean }>("POST", "/api/links/retry"),
  reprocessLink: (url: string) => request<{ ok: boolean }>("POST", "/api/links/reprocess", { url }),
  ingestLinks: (links: { url: string; note: string }[]) =>
    request<{ job: string; added: number }>("POST", "/api/ingest/links", { links }),
  ingestFamily: (family: string, sources: string[]) =>
    request<{ job: string }>("POST", `/api/ingest/${family}`, { sources }),
  areas: () => request<Area[]>("GET", "/api/areas"),
  createArea: (label: string, description: string, color: string) =>
    request<Area>("POST", "/api/areas", { label, description, color }),
  updateArea: (name: string, changes: Partial<Pick<Area, "label" | "description" | "color" | "exclude_from_vault">>) =>
    request<Area>("PUT", `/api/areas/${name}`, changes),
  deleteArea: (name: string) => request<{ moved: number }>("DELETE", `/api/areas/${name}`),
  approveArea: (name: string, changes: { label: string; description: string; color: string }) =>
    request<{ job: string }>("POST", `/api/areas/${name}/approve`, changes),
  rejectArea: (name: string) => request<{ ok: boolean }>("POST", `/api/areas/${name}/reject`, {}),
  cleanVault: (name: string) => request<{ edited: string[]; errors: string[] }>("POST", `/api/areas/${name}/vault-purge`, {}),
  assignArea: (kind: DeleteKind, id: string, area: string) =>
    request<{ area: string; area_origin: TagOrigin }>("POST", "/api/areas/assign", { kind, id, area }),
  linkDetail: (url: string) => request<LinkDetail>("GET", `/api/links/detail?url=${encodeURIComponent(url)}`),
  uploadFile: async (file: File, note: string) => {
    const endpoint = file.type === "application/pdf" ? "documents" : "images";
    const response = await fetch(`/api/uploads/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": file.type, "X-Note": encodeURIComponent(note) },
      body: file,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(`${file.name || "screenshot"}: ${(data as { error?: string }).error ?? response.status}`);
    return data as { name: string; url: string; added: boolean };
  },
  editTags: (kind: DeleteKind, id: string, add: string[], remove: string[]) =>
    request<{ tags: string[]; tag_origin: Record<string, TagOrigin> }>("POST", "/api/tags", { kind, id, add, remove }),
  deleteImpact: (kind: DeleteKind, id: string) =>
    request<Impact>("GET", `/api/documents/impact?kind=${kind}&id=${encodeURIComponent(id)}`),
  deleteDocument: (kind: DeleteKind, id: string, token: string) =>
    request<DeleteResult>("DELETE", "/api/documents", { kind, id, token }),
  job: (id: string) => request<JobState>("GET", `/api/jobs/${id}`),
  cancelJob: (id: string) => request<{ ok: boolean }>("POST", `/api/jobs/${id}/cancel`, {}),
  runningJob: () => request<{ running: JobState | null }>("GET", "/api/jobs"),
};
