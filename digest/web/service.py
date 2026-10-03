from __future__ import annotations

import functools
import itertools
import json
import re
import subprocess
import sys
import threading
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from ..config import Config
from ..media import CONTENT_TYPE_BY_EXT, MEDIA_NAME, store_upload
from ..sources.documents import DOC_NAME, DOC_SCHEME, store_pdf
from ..sources.links import IMAGE_SCHEME
from ..purge import NotFound, Purger
from ..state import FALLBACK_AREA, State
from ..tags import GENERIC_TAGS, ORIGIN_AUTO, ORIGIN_MANUAL, ORIGIN_MODEL, Vocabulary, canonical
from ..util import iso_z, utcnow

FAMILIES: dict[str, list[str]] = {
    "links": ["links", "notion"],
    "chat": ["teams", "slack"],
    "mail": ["mail", "gmail"],
    "tickets": ["jira", "slack_tickets"],
}
SOURCE_LABELS = {
    "links": "Link salvati", "notion": "Notion", "teams": "Microsoft Teams", "slack": "Slack",
    "mail": "Outlook", "gmail": "Gmail", "jira": "Jira", "slack_tickets": "Slack (ticket)",
}
MAX_ITEM_NODES = 300
MAX_AREA_NAME = 40
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#9ca3af"
MAX_GROUP_FOR_PAIRS = 30
LOG_TAIL_LINES = 200


def normalize_tag(tag: str) -> str:
    return canonical(tag)  # 20261002 ** RG #tags_vocabulary one normalization for the whole app


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def with_state(method):
    """Open a State for the call and always close it (SQLite keeps the file locked on Windows)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._state() as state:
            return method(self, state, *args, **kwargs)

    return wrapper


class Job:
    def __init__(self, sources: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.sources = sources
        self.status = "running"
        self.exit_code: int | None = None
        self.started_at = iso_z(utcnow())
        self.finished_at: str | None = None
        self.lines: list[str] = []

    def as_dict(self) -> dict:
        return {
            "id": self.id, "sources": self.sources, "status": self.status, "exit_code": self.exit_code,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "log": self.lines[-LOG_TAIL_LINES:],
        }


class Service:
    def __init__(self, cfg: Config, config_path: Path):
        self.cfg = cfg
        self.config_path = config_path
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _state(self) -> State:
        return State(self.cfg.data_path / "state.sqlite")

    def _is_configured(self, name: str) -> bool:
        graph_ready = bool(self.cfg.graph.client_id)
        checks = {
            "links": lambda: True,
            "notion": lambda: bool(self.cfg.notion.token),
            "teams": lambda: graph_ready,
            "mail": lambda: graph_ready,
        }
        if name in checks:
            return checks[name]()
        section = getattr(self.cfg, name.split("_")[0], None)
        if name == "slack_tickets":
            return bool(section and section.is_configured() and section.ticket_channels)
        return bool(section and section.is_configured())

    def _is_enabled(self, name: str) -> bool:
        section = getattr(self.cfg, name.split("_")[0], None)
        return bool(section and getattr(section, "enabled", False))

    @with_state
    def sources_status(self, state) -> list[dict]:
        last_seen = dict(state.db.execute("SELECT source, MAX(seen_at) FROM seen GROUP BY source"))
        counts = dict(state.db.execute("SELECT source, COUNT(*) FROM seen GROUP BY source"))
        link_row = state.db.execute(
            "SELECT COUNT(*), MAX(processed_at) FROM links WHERE status='done'"
        ).fetchone()
        out = []
        for family, names in FAMILIES.items():
            for name in names:
                if name == "links":
                    docs, last = link_row[0], link_row[1]
                else:
                    docs, last = counts.get(name, 0), last_seen.get(name)
                out.append({
                    "name": name, "label": SOURCE_LABELS[name], "family": family,
                    "enabled": self._is_enabled(name), "configured": self._is_configured(name),
                    "documents": docs, "last_import": last,
                })
        return out

    @with_state
    def stats(self, state, days: int = 30) -> dict:
        since = iso_z(utcnow() - timedelta(days=days))[:10]
        per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for day, n in state.db.execute(
            "SELECT substr(processed_at,1,10), COUNT(*) FROM links WHERE status='done' AND processed_at>=? GROUP BY 1",
            (since,),
        ):
            per_day[day]["links"] += n
        for day, source, n in state.db.execute(
            "SELECT substr(seen_at,1,10), source, COUNT(*) FROM seen WHERE seen_at>=? GROUP BY 1, 2", (since,)
        ):
            per_day[day][source] += n
        series = [{"date": d, "counts": dict(c), "total": sum(c.values())} for d, c in sorted(per_day.items())]

        link_status = dict(state.db.execute("SELECT status, COUNT(*) FROM links GROUP BY status"))
        item_kinds = dict(state.db.execute("SELECT kind, COUNT(*) FROM items GROUP BY kind"))
        last_run = state.db.execute("SELECT MAX(run_id) FROM items").fetchone()[0]
        sources = self.sources_status()
        return {
            "totals": {
                "documents": sum(s["documents"] for s in sources),
                "items": sum(item_kinds.values()),
                "links_pending": link_status.get("pending", 0) + link_status.get("error", 0),
            },
            "by_source": {s["name"]: s["documents"] for s in sources if s["documents"]},
            "by_area": state.area_counts(),
            "item_kinds": item_kinds,
            "last_import": max((s["last_import"] for s in sources if s["last_import"]), default=None),
            "last_run": last_run,
            "per_day": series,
        }

    @with_state
    def list_links(self, state, limit: int = 200) -> list[dict]:
        rows = state.db.execute(
            "SELECT url, COALESCE(note,''), added_at, status, attempts, processed_at, COALESCE(error,''), "
            "COALESCE(title,'') FROM links ORDER BY added_at DESC LIMIT ?", (limit,),
        ).fetchall()
        keys = ("url", "note", "added_at", "status", "attempts", "processed_at", "error", "title")
        links = [dict(zip(keys, r)) for r in rows]
        for entry in links:
            analysis = state.link_analysis(entry["url"]) or {}
            entry["tags"], entry["tag_origin"] = analysis.get("tags", []), analysis.get("tag_origin") or {}
            entry["image"] = state.link_image(entry["url"])
        return links

    @with_state
    def add_links(self, state, links: list[dict]) -> int:
        added = 0
        with self.cfg.links_path.open("a", encoding="utf-8") as fh:
            for entry in links:
                url = str(entry.get("url", "")).strip()
                note = str(entry.get("note", "")).strip()
                if not url.lower().startswith(("http://", "https://")):
                    continue
                if state.add_link(url, note):
                    fh.write(url + (f"  {note}" if note else "") + "\n")
                    added += 1
        return added

    @with_state
    def areas(self, state) -> list[dict]:
        counts = state.area_counts()
        return [a | {"count": counts.get(a["name"], 0)} for a in state.list_areas()]

    @with_state
    def create_area(self, state, label: str, description: str, color: str) -> dict:
        label = " ".join(label.split())
        name = canonical(label)
        if (not re.search(r"[a-z]", name) or len(label) > MAX_AREA_NAME
                or not HEX_COLOR.match(color or DEFAULT_COLOR)):
            raise ValueError("an area needs a short name and a #rrggbb colour")
        existing = state.get_area(name)
        if existing and existing["status"] == "active":
            raise ValueError(f"the area {existing['label']!r} already exists")
        state.save_area(name, label, description.strip(), color or DEFAULT_COLOR)  # the user's own: no approval
        return state.get_area(name)

    @with_state
    def update_area(self, state, name: str, changes: dict) -> dict:
        area = state.get_area(name)
        if area is None:
            raise NotFound(f"area not found: {name}")
        color = changes.get("color", area["color"])
        if not HEX_COLOR.match(color):
            raise ValueError("colour must be #rrggbb")
        label = " ".join(str(changes.get("label", area["label"])).split()) or area["label"]
        if area["system"] and changes.get("exclude_from_vault"):
            raise ValueError("the fallback area cannot be kept out of the vault")
        state.save_area(name, label, str(changes.get("description", area["description"])).strip(), color,
                        area["status"], area["why"], bool(changes.get("exclude_from_vault", area["exclude_from_vault"])))
        return state.get_area(name)

    @with_state
    def delete_area(self, state, name: str) -> dict:
        area = state.get_area(name)
        if area is None:
            raise NotFound(f"area not found: {name}")
        if area["system"]:
            raise ValueError("the fallback area cannot be deleted")
        return {"moved": state.delete_area(name)}

    @with_state
    def reject_area(self, state, name: str) -> None:
        area = state.get_area(name)
        if area is None or area["status"] != "proposed":
            raise NotFound(f"no proposal named {name}")
        state.set_area_status(name, "rejected")

    def approve_area(self, name: str, changes: dict) -> Job:
        if self.running_job():
            raise RuntimeError("another job is running: wait for it to finish before approving")
        with self._state() as state:
            area = state.get_area(name)
            if area is None or area["status"] != "proposed":
                raise NotFound(f"no proposal named {name}")
            color = changes.get("color") or area["color"] or DEFAULT_COLOR
            if not HEX_COLOR.match(color):
                raise ValueError("colour must be #rrggbb")
            state.save_area(name, " ".join(str(changes.get("label") or area["label"]).split()),
                            str(changes.get("description") or area["description"]).strip(), color, "active", area["why"])
        return self._start_job([f"area:{name}"], ["reclassify", "--area", name])

    @with_state
    def assign_area(self, state, kind: str, ident: str, area: str) -> dict:
        target = state.get_area(area)
        if target is None or target["status"] != "active":
            raise ValueError(f"{area!r} is not an active area")
        if kind == "link":
            if state.link_analysis(ident) is None:
                raise NotFound(f"link not analysed: {ident}")
            state.set_link_area(ident, area, ORIGIN_MANUAL)
        elif kind in ("item", "doc"):
            item_ids = [int(ident)] if kind == "item" else state.item_ids_for_ref(ident)
            if not item_ids:
                raise NotFound(f"nothing to assign for {kind} {ident}")
            for item_id in item_ids:
                state.set_item_area(item_id, area, ORIGIN_MANUAL)
        else:
            raise ValueError(f"unknown kind: {kind}")
        return {"area": area, "area_origin": ORIGIN_MANUAL}

    @with_state
    def clean_vault_for_area(self, state, name: str) -> dict:
        if self.running_job():
            raise RuntimeError("an ingest job is running: wait for it to finish")
        if state.get_area(name) is None:
            raise NotFound(f"area not found: {name}")
        return Purger(self.cfg, state).vault_cleanup(name)

    # 20261002 ++ RG #recipes put an already digested link back in the queue (manual tags are kept by link_done)
    @with_state
    def reprocess_link(self, state, url: str) -> None:
        if state.link_row(url) is None:
            raise NotFound(f"link not found: {url}")
        state.reset_link(url)

    @with_state
    def retry_failed_links(self, state) -> None:
        state.db.execute("UPDATE links SET status='pending', attempts=0, error=NULL WHERE status='error'")
        state.db.commit()

    @with_state
    def link_tags(self, state) -> dict:
        counts = state.tag_counts()  # 20261002 ** RG #tags_for_everything links + items, auto tags excluded
        available = [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        return {"linking": state.get_link_tags(), "available": available}

    @with_state
    def edit_tags(self, state, kind: str, ident: str, add: list[str], remove: list[str]) -> dict:
        vocabulary = Vocabulary(state.tag_counts(), state.get_link_tags())
        to_add = [t for t in (vocabulary.resolve(x) for x in add) if t and t not in GENERIC_TAGS]
        to_remove = {canonical(x) for x in remove}
        if kind == "link":
            analysis = state.link_analysis(ident)
            if analysis is None:
                raise NotFound(f"link not analysed: {ident}")
            tags, origin = self._edited(analysis.get("tags", []), analysis.get("tag_origin") or {}, to_add, to_remove)
            state.set_link_analysis(ident, {**analysis, "tags": tags, "tag_origin": origin})
            return {"tags": tags, "tag_origin": origin}
        item_ids = [int(ident)] if kind == "item" else state.item_ids_for_ref(ident) if kind == "doc" else []
        if not item_ids:
            raise NotFound(f"nothing to tag for {kind} {ident}")
        result: dict = {}
        for item_id in item_ids:
            current = state.item_tags(item_id)
            if current is None:
                raise NotFound(f"item {item_id} not found")
            tags, origin = self._edited(current[0], current[1], to_add, to_remove)
            state.set_item_tags(item_id, tags, origin)
            result = {"tags": tags, "tag_origin": origin}
        return result

    @staticmethod
    def _edited(tags: list[str], origin: dict[str, str], to_add: list[str], to_remove: set[str]):
        origin = dict(origin)
        kept = [t for t in tags if t not in to_remove]
        for tag in to_add:
            if tag not in kept:
                kept.append(tag)
                origin[tag] = ORIGIN_MANUAL
        if any(origin.get(t, ORIGIN_MODEL) != ORIGIN_AUTO for t in kept):
            kept = [t for t in kept if origin.get(t, ORIGIN_MODEL) != ORIGIN_AUTO]  # placeholders go once real tags exist
        if not kept:
            raise ValueError("every piece of information needs at least one tag")
        return kept, {t: origin.get(t, ORIGIN_MODEL) for t in kept}

    @with_state
    def set_link_tags(self, state, tags: list[str]) -> list[str]:
        cleaned = sorted({normalize_tag(t) for t in tags if normalize_tag(t)})
        state.set_link_tags(cleaned)
        return cleaned

    @with_state
    def graph(self, state, include_domain: bool = False) -> dict:
        linking = set(state.get_link_tags())
        nodes: dict[str, dict] = {}

        for url, title, processed_at, raw, image in state.db.execute(
            "SELECT url, COALESCE(title,''), processed_at, analysis, image FROM links WHERE status='done'"
        ):
            analysis = json.loads(raw) if raw else {}
            tags = [normalize_tag(t) for t in analysis.get("tags", [])]
            nodes[url] = {"id": url, "type": "doc", "source": "links", "label": title or url, "url": url,
                          "tags": tags, "tag_origin": analysis.get("tag_origin") or {}, "timestamp": processed_at,
                          "image": image, "area": analysis.get("area") or FALLBACK_AREA,
                          "area_origin": analysis.get("area_origin") or "auto"}

        item_rows = state.db.execute(
            "SELECT id, source, kind, title, ref_title, ref_url, tags, tag_origin, area, area_origin FROM items "
            "ORDER BY id DESC LIMIT ?", (MAX_ITEM_NODES,),
        ).fetchall()
        edges: list[dict] = []
        ref_pairs: set[frozenset[str]] = set()
        doc_areas: dict[str, list[str]] = defaultdict(list)
        for item_id, source, kind, title, ref_title, ref_url, tags_raw, origin_raw, area, area_origin in item_rows:
            node_id = f"item:{item_id}"
            tags, origin = json.loads(tags_raw or "[]"), json.loads(origin_raw or "{}")
            nodes[node_id] = {"id": node_id, "type": "item", "source": source, "label": title, "kind": kind,
                              "url": ref_url or "", "tags": tags, "tag_origin": origin,
                              "area": area or FALLBACK_AREA, "area_origin": area_origin or "auto"}
            if ref_url:
                doc_areas[ref_url].append(area or FALLBACK_AREA)
            if ref_url:
                doc = nodes.setdefault(ref_url, {"id": ref_url, "type": "doc", "source": source,
                                                 "label": ref_title or ref_url, "url": ref_url,
                                                 "tags": [], "tag_origin": {}, "area": FALLBACK_AREA,
                                                 "area_origin": "auto"})
                if doc["source"] != "links":  # a doc built from items inherits the union of their tags
                    for tag in tags:
                        if tag not in doc["tags"]:
                            doc["tags"].append(tag)
                        if doc["tag_origin"].get(tag) != ORIGIN_MODEL and tag in origin:
                            doc["tag_origin"][tag] = origin[tag]
                edges.append({"source": node_id, "target": ref_url, "type": "ref"})
                ref_pairs.add(frozenset((node_id, ref_url)))

        for ref_url, areas in doc_areas.items():  # a doc built from items takes the most common area of its items
            doc = nodes[ref_url]
            if doc["source"] != "links":
                doc["area"] = max(dict.fromkeys(areas), key=areas.count)
                doc["area_origin"] = "model"
        docs = [n for n in nodes.values() if n["type"] == "doc"]
        edges += self._tag_edges(list(nodes.values()), linking, ref_pairs)
        if include_domain:
            edges += self._domain_edges(docs)
        return {"nodes": list(nodes.values()), "edges": edges, "linking_tags": sorted(linking)}

    @staticmethod
    def _tag_edges(nodes: list[dict], linking: set[str], ref_pairs: set[frozenset[str]] = frozenset()) -> list[dict]:
        by_tag: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for tag in set(node["tags"]) & linking:
                by_tag[tag].append(node["id"])
        merged: dict[tuple[str, str], list[str]] = defaultdict(list)
        for tag, ids in by_tag.items():
            if len(ids) > MAX_GROUP_FOR_PAIRS:
                continue
            for a, b in itertools.combinations(sorted(ids), 2):
                if frozenset((a, b)) not in ref_pairs:  # an item and its own doc are already linked by "ref"
                    merged[(a, b)].append(tag)
        return [{"source": a, "target": b, "type": "tag", "tags": sorted(tags)} for (a, b), tags in merged.items()]

    @staticmethod
    def _domain_edges(docs: list[dict]) -> list[dict]:
        by_domain: dict[str, list[str]] = defaultdict(list)
        for doc in docs:
            domain = domain_of(doc["url"])
            if domain:
                by_domain[domain].append(doc["id"])
        edges = []
        for domain, ids in by_domain.items():
            if len(ids) > MAX_GROUP_FOR_PAIRS:
                continue
            edges += [{"source": a, "target": b, "type": "domain", "domain": domain}
                      for a, b in itertools.combinations(sorted(ids), 2)]
        return edges

    @with_state
    def link_detail(self, state, url: str) -> dict:
        analysis = state.link_analysis(url)
        if analysis is None:
            raise NotFound(f"no analysis for {url}")
        keys = ("kind", "title", "platform", "note", "summary", "key_points", "actions", "worth_it", "recipe")
        return {key: analysis.get(key) for key in keys} | {"kind": analysis.get("kind") or "link"}

    @with_state
    def queue_screenshot(self, state, data: bytes, note: str) -> dict:
        name = store_upload(self.cfg.data_path / "media", data, self.cfg.links.max_screenshot_bytes)
        return self._enqueue(state, f"{IMAGE_SCHEME}{name}", name, note)

    @with_state
    def queue_document(self, state, data: bytes, note: str) -> dict:
        name = store_pdf(self.cfg.data_path / "docs", data, self.cfg.links.max_pdf_bytes)
        return self._enqueue(state, f"{DOC_SCHEME}{name}", name, note)

    @staticmethod
    def _enqueue(state: State, url: str, name: str, note: str) -> dict:
        added = state.add_link(url, note.strip()[:200])
        if not added and (state.link_row(url) or {}).get("status") == "error":
            state.reset_link(url)  # uploading the same file again retries a failed one
            added = True
        return {"name": name, "url": url, "added": added}

    def doc_file(self, name: str) -> bytes | None:
        """Bytes of a stored PDF. Only our own content-addressed names are ever served."""
        path = self.cfg.data_path / "docs" / name
        return path.read_bytes() if DOC_NAME.match(name) and path.is_file() else None

    def media_file(self, name: str) -> tuple[bytes, str] | None:
        """Bytes + content type of a stored image. Only our own content-addressed names are ever served."""
        if not MEDIA_NAME.match(name):
            return None
        path = self.cfg.data_path / "media" / name
        if not path.is_file():
            return None
        return path.read_bytes(), CONTENT_TYPE_BY_EXT[name.rsplit(".", 1)[1]]

    def delete_impact(self, kind: str, ident: str) -> dict:
        with self._state() as state:
            purger = Purger(self.cfg, state)
            return purger.describe(purger.plan(kind, ident))

    def delete_document(self, kind: str, ident: str, token: str) -> dict:
        if self.running_job():
            raise RuntimeError("an ingest job is running: wait for it to finish before deleting")
        with self._state() as state:
            return Purger(self.cfg, state).execute(kind, ident, token)

    def running_job(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status == "running"), None)

    def start_ingest(self, sources: list[str]) -> Job:
        known = {n for names in FAMILIES.values() for n in names}
        if not sources or set(sources) - known:
            raise ValueError(f"unknown sources: {sorted(set(sources) - known)}")
        with self._lock:
            if self.running_job():
                raise RuntimeError("another job is already running")
            job = Job(sources)
            self.jobs[job.id] = job
        command = [sys.executable, "-m", "digest", "-c", str(self.config_path), "run",
                   "--sources", ",".join(sources)]
        threading.Thread(target=self._run_job, args=(job, command), daemon=True).start()
        return job

    def _start_job(self, label: list[str], arguments: list[str]) -> Job:
        with self._lock:
            if self.running_job():
                raise RuntimeError("another job is already running")
            job = Job(label)
            self.jobs[job.id] = job
        command = [sys.executable, "-m", "digest", "-c", str(self.config_path), *arguments]
        threading.Thread(target=self._run_job, args=(job, command), daemon=True).start()
        return job

    def _run_job(self, job: Job, command: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                command, cwd=self.cfg.base_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                job.lines.append(line.rstrip())
            job.exit_code = proc.wait()
            job.status = "done" if job.exit_code == 0 else "failed"
        except OSError as e:
            job.lines.append(f"cannot start job: {e}")
            job.status = "failed"
        finally:
            job.finished_at = iso_z(utcnow())

    def job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return job.as_dict() if job else None
