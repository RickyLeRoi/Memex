from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .tags import ORIGIN_AUTO, Vocabulary
from .util import iso_z, utcnow

FALLBACK_AREA = "altro"
MAX_EVIDENCE = 5
# (name, label, description for the model, color, system)
DEFAULT_AREAS = (
    ("ricette", "Ricette", "ricette di cucina, cibo, ingredienti, menu", "#e07a3f", 0),
    ("software", "Software", "programmazione, strumenti, librerie, IoT, tecnologia", "#3b82f6", 0),
    ("posti", "Posti", "luoghi da visitare, viaggi, ristoranti, indirizzi", "#10b981", 0),
    ("progetti", "Progetti", "progetti personali o di gruppo, idee da realizzare", "#8b5cf6", 0),
    ("lavoro", "Lavoro", "attività, clienti, scadenze e comunicazioni di lavoro", "#64748b", 0),
    ("documenti", "Documenti", "documenti personali, della casa o dell'auto: contratti, assicurazioni, fatture, bollette",
     "#eab308", 0),
    ("salute", "Salute", "salute, medicina, sport, alimentazione, benessere", "#ef4444", 0),
    (FALLBACK_AREA, "Altro", "tutto ciò che non rientra nelle altre aree", "#9ca3af", 1),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, id TEXT NOT NULL, fp TEXT, seen_at TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS links (
    url TEXT PRIMARY KEY, note TEXT, added_at TEXT, status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0, processed_at TEXT, error TEXT, title TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT, kind TEXT, title TEXT,
    details TEXT, owner TEXT, due TEXT, priority TEXT, ref_title TEXT, ref_url TEXT, raw TEXT,
    tags TEXT, tag_origin TEXT, area TEXT, area_origin TEXT
);
CREATE TABLE IF NOT EXISTS areas (
    name TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT DEFAULT '', color TEXT DEFAULT '#9ca3af',
    status TEXT NOT NULL DEFAULT 'active', exclude_from_vault INTEGER DEFAULT 0, system INTEGER DEFAULT 0,
    proposals INTEGER DEFAULT 0, why TEXT DEFAULT '', evidence TEXT DEFAULT '[]', created_at TEXT
);
"""


class State:
    def __init__(self, path: Path, ignore_seen: bool = False):
        self.ignore_seen = ignore_seen
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(links)")}
        if "analysis" not in columns:
            self.db.execute("ALTER TABLE links ADD COLUMN analysis TEXT")
        if "image" not in columns:
            self.db.execute("ALTER TABLE links ADD COLUMN image TEXT")
        item_columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        for column in ("tags", "tag_origin", "area", "area_origin"):
            if column not in item_columns:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
        self._backfill_item_tags()
        self._backfill_link_tags()
        self._seed_areas()
        self.db.commit()

    # 20261002 ++ RG #gui connections must be closed explicitly (file lock on Windows)
    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_cursor(self, source: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (f"cursor:{source}",)).fetchone()
        return row[0] if row else None

    def set_cursor(self, source: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"cursor:{source}", value),
        )
        self.db.commit()

    def seen_fp(self, source: str, item_id: str) -> str | None:
        if self.ignore_seen:
            return None
        row = self.db.execute("SELECT fp FROM seen WHERE source=? AND id=?", (source, item_id)).fetchone()
        return row[0] if row else None

    def is_seen(self, source: str, item_id: str) -> bool:
        if self.ignore_seen:
            return False
        return self.db.execute(
            "SELECT 1 FROM seen WHERE source=? AND id=?", (source, item_id)
        ).fetchone() is not None

    def mark_seen(self, source: str, ids: list[tuple[str, str | None]]) -> None:
        now = iso_z(utcnow())
        self.db.executemany(
            "INSERT INTO seen(source, id, fp, seen_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(source, id) DO UPDATE SET fp=excluded.fp, seen_at=excluded.seen_at",
            [(source, i, fp, now) for i, fp in ids],
        )
        self.db.commit()

    def add_link(self, url: str, note: str = "") -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO links(url, note, added_at) VALUES(?, ?, ?)", (url, note, iso_z(utcnow()))
        )
        self.db.commit()
        return cur.rowcount > 0

    def pending_links(self, max_attempts: int) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT url, COALESCE(note, '') FROM links WHERE status != 'done' AND attempts < ? ORDER BY added_at",
            (max_attempts,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # 20261002 ** RG #gui_link_analysis persist the analysis so the graph can read tags
    def link_done(self, url: str, title: str, analysis: dict | None = None) -> None:
        analysis = self._with_tags_kept(url, analysis)
        payload = json.dumps(analysis, ensure_ascii=False) if analysis else None
        image = (analysis or {}).get("image")  # 20261002 ++ RG #images a reprocess without image keeps the old one
        self.db.execute(
            "UPDATE links SET status='done', processed_at=?, title=?, analysis=?, error=NULL, "
            "image=COALESCE(?, image), attempts=attempts+1 WHERE url=?",
            (iso_z(utcnow()), title, payload, image, url),
        )
        self.db.commit()

    def link_failed(self, url: str, error: str) -> None:
        self.db.execute(
            "UPDATE links SET status='error', error=?, attempts=attempts+1 WHERE url=?", (error[:500], url)
        )
        self.db.commit()

    def reset_link(self, url: str) -> None:
        self.db.execute("UPDATE links SET status='pending', attempts=0, error=NULL WHERE url=?", (url,))
        self.db.commit()

    # 20261002 ++ RG #gui_link_tags curated allowlist: only these tags create graph edges
    def get_link_tags(self) -> list[str]:
        row = self.db.execute("SELECT value FROM kv WHERE key='graph:link_tags'").fetchone()
        return json.loads(row[0]) if row else []

    def set_link_tags(self, tags: list[str]) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES('graph:link_tags', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(tags, ensure_ascii=False),),
        )
        self.db.commit()

    @staticmethod
    def _tag_columns(item: dict) -> tuple[str, str]:
        tags, origin = item.get("tags") or [], item.get("tag_origin") or {}
        if not tags:  # last line of defence: no item is stored untagged
            tags, origin = Vocabulary().finalize(None, source=item.get("source") or "", kind=item.get("kind") or "")
        return json.dumps(tags, ensure_ascii=False), json.dumps(origin, ensure_ascii=False)

    def _with_tags_kept(self, url: str, analysis: dict | None) -> dict | None:
        """Reprocessing replaces the analysis but keeps manual tags; an analysis is never left without tags."""
        if analysis is None:
            return None
        analysis = dict(analysis)
        tags = list(analysis.get("tags") or [])
        origin = dict(analysis.get("tag_origin") or {t: "model" for t in tags})
        previous = self.link_analysis(url) or {}
        if previous.get("area_origin") == "manual":  # 20261002 ++ RG #areas an area chosen by hand is never overwritten
            analysis["area"], analysis["area_origin"] = previous["area"], "manual"
        for tag in previous.get("tags", []):
            if (previous.get("tag_origin") or {}).get(tag) == "manual" and tag not in tags:
                tags.append(tag)
                origin[tag] = "manual"
        if not tags:
            tags, origin = Vocabulary().finalize(None, source="link", fallback_extra=(analysis.get("platform", ""),))
        analysis["tags"], analysis["tag_origin"] = tags, origin
        return analysis

    def _backfill_link_tags(self) -> None:
        rows = self.db.execute("SELECT url, COALESCE(title, '') FROM links WHERE status='done' AND analysis IS NULL")
        for url, title in rows.fetchall():
            domain = urlparse(url).netloc.removeprefix("www.")
            tags, origin = Vocabulary().finalize(None, source="link", fallback_extra=(domain,))
            payload = json.dumps({"title": title, "url": url, "tags": tags, "tag_origin": origin}, ensure_ascii=False)
            self.db.execute("UPDATE links SET analysis=? WHERE url=?", (payload, url))

    def _backfill_item_tags(self) -> None:
        rows = self.db.execute("SELECT id, source, kind FROM items WHERE tags IS NULL").fetchall()
        for item_id, source, kind in rows:
            tags, origin = Vocabulary().finalize(None, source=source or "", kind=kind or "")
            self.db.execute("UPDATE items SET tags=?, tag_origin=? WHERE id=?",
                            (json.dumps(tags), json.dumps(origin), item_id))

    def tag_counts(self) -> dict[str, int]:
        """Tags in use (links + items). Auto tags are excluded: they must not feed the LLM vocabulary."""
        counts: dict[str, int] = {}
        for (raw,) in self.db.execute("SELECT analysis FROM links WHERE analysis IS NOT NULL"):
            analysis = json.loads(raw)
            origin = analysis.get("tag_origin") or {}
            for tag in analysis.get("tags", []):
                if origin.get(tag) != ORIGIN_AUTO:
                    counts[tag] = counts.get(tag, 0) + 1
        for tags_raw, origin_raw in self.db.execute("SELECT tags, tag_origin FROM items WHERE tags IS NOT NULL"):
            origin = json.loads(origin_raw or "{}")
            for tag in json.loads(tags_raw):
                if origin.get(tag) != ORIGIN_AUTO:
                    counts[tag] = counts.get(tag, 0) + 1
        return counts

    def item_tags(self, item_id: int) -> tuple[list[str], dict[str, str]] | None:
        row = self.db.execute("SELECT tags, tag_origin FROM items WHERE id=?", (item_id,)).fetchone()
        return (json.loads(row[0] or "[]"), json.loads(row[1] or "{}")) if row else None

    def set_item_tags(self, item_id: int, tags: list[str], origin: dict[str, str]) -> None:
        self.db.execute("UPDATE items SET tags=?, tag_origin=? WHERE id=?",
                        (json.dumps(tags, ensure_ascii=False), json.dumps(origin, ensure_ascii=False), item_id))
        self.db.commit()

    def item_ids_for_ref(self, ref_url: str) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT id FROM items WHERE ref_url=?", (ref_url,))]

    def link_analysis(self, url: str) -> dict | None:
        row = self.db.execute("SELECT analysis FROM links WHERE url=?", (url,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def set_link_analysis(self, url: str, analysis: dict) -> None:
        self.db.execute("UPDATE links SET analysis=? WHERE url=?", (json.dumps(analysis, ensure_ascii=False), url))
        self.db.commit()

    def _seed_areas(self) -> None:
        """First run: the user's starting list of areas. 'altro' always exists (fallback, never deletable)."""
        for name, label, description, color, system in DEFAULT_AREAS:
            self.db.execute(
                "INSERT OR IGNORE INTO areas(name, label, description, color, system, created_at) VALUES(?,?,?,?,?,?)",
                (name, label, description, color, system, iso_z(utcnow())),
            )
        self.db.execute("UPDATE items SET area=?, area_origin=? WHERE area IS NULL", (FALLBACK_AREA, "auto"))
        for url, raw in self.db.execute("SELECT url, analysis FROM links WHERE analysis IS NOT NULL").fetchall():
            analysis = json.loads(raw)
            if "area" not in analysis:
                analysis.update(area=FALLBACK_AREA, area_origin="auto")
                self.db.execute("UPDATE links SET analysis=? WHERE url=?", (json.dumps(analysis, ensure_ascii=False), url))

    def list_areas(self) -> list[dict]:
        keys = ("name", "label", "description", "color", "status", "exclude_from_vault", "system", "proposals",
                "why", "evidence")
        rows = self.db.execute(
            f"SELECT {', '.join(keys)} FROM areas ORDER BY system, rowid").fetchall()
        return [dict(zip(keys, r)) | {"evidence": json.loads(r[-1] or "[]"), "exclude_from_vault": bool(r[5]),
                                      "system": bool(r[6])} for r in rows]

    def get_area(self, name: str) -> dict | None:
        return next((a for a in self.list_areas() if a["name"] == name), None)

    def save_area(self, name: str, label: str, description: str, color: str, status: str = "active",
                  why: str = "", exclude_from_vault: bool = False) -> None:
        self.db.execute(
            "INSERT INTO areas(name, label, description, color, status, why, exclude_from_vault, created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET label=excluded.label, "
            "description=excluded.description, color=excluded.color, status=excluded.status, "
            "exclude_from_vault=excluded.exclude_from_vault",
            (name, label, description, color, status, why, int(exclude_from_vault), iso_z(utcnow())),
        )
        self.db.commit()

    def set_area_status(self, name: str, status: str) -> None:
        self.db.execute("UPDATE areas SET status=? WHERE name=?", (status, name))
        self.db.commit()

    def add_area_proposal(self, name: str, label: str, why: str, example: str) -> None:
        row = self.db.execute("SELECT status, evidence FROM areas WHERE name=?", (name,)).fetchone()
        if row and row[0] != "proposed":
            return
        if row:
            evidence = json.loads(row[1] or "[]")
            if example and example not in evidence and len(evidence) < MAX_EVIDENCE:
                evidence.append(example)
            self.db.execute("UPDATE areas SET proposals=proposals+1, evidence=? WHERE name=?",
                            (json.dumps(evidence, ensure_ascii=False), name))
        else:
            self.db.execute(
                "INSERT INTO areas(name, label, description, status, why, proposals, evidence, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (name, label, why, "proposed", why, 1, json.dumps([example] if example else [], ensure_ascii=False),
                 iso_z(utcnow())),
            )
        self.db.commit()

    def delete_area(self, name: str) -> int:
        moved = 0
        with self.db:
            moved += self.db.execute("UPDATE items SET area=?, area_origin='auto' WHERE area=?",
                                     (FALLBACK_AREA, name)).rowcount
            for url, raw in self.db.execute("SELECT url, analysis FROM links WHERE analysis IS NOT NULL").fetchall():
                analysis = json.loads(raw)
                if analysis.get("area") == name:
                    analysis.update(area=FALLBACK_AREA, area_origin="auto")
                    self.db.execute("UPDATE links SET analysis=? WHERE url=?",
                                    (json.dumps(analysis, ensure_ascii=False), url))
                    moved += 1
            self.db.execute("DELETE FROM areas WHERE name=? AND system=0", (name,))
        return moved

    def area_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for (raw,) in self.db.execute("SELECT analysis FROM links WHERE status='done' AND analysis IS NOT NULL"):
            area = json.loads(raw).get("area") or FALLBACK_AREA
            counts[area] = counts.get(area, 0) + 1
        for area, n in self.db.execute("SELECT COALESCE(area, ?), COUNT(*) FROM items GROUP BY 1", (FALLBACK_AREA,)):
            counts[area] = counts.get(area, 0) + n
        return counts

    def set_link_area(self, url: str, area: str, origin: str) -> None:
        analysis = self.link_analysis(url)
        if analysis is None:
            return
        self.set_link_analysis(url, {**analysis, "area": area, "area_origin": origin})

    def set_item_area(self, item_id: int, area: str, origin: str) -> None:
        self.db.execute("UPDATE items SET area=?, area_origin=? WHERE id=?", (area, origin, item_id))
        self.db.commit()

    def link_image(self, url: str) -> str | None:
        row = self.db.execute("SELECT image FROM links WHERE url=?", (url,)).fetchone()
        return row[0] if row and row[0] else None

    def image_users(self, name: str, excluding_urls: set[str] = frozenset()) -> int:
        rows = self.db.execute("SELECT url FROM links WHERE image=?", (name,)).fetchall()
        return sum(1 for (url,) in rows if url not in excluding_urls)

    def link_attempts(self, url: str) -> int:
        row = self.db.execute("SELECT attempts FROM links WHERE url=?", (url,)).fetchone()
        return row[0] if row and row[0] else 0

    def link_row(self, url: str) -> dict | None:
        row = self.db.execute("SELECT url, COALESCE(title, ''), status FROM links WHERE url=?", (url,)).fetchone()
        return dict(zip(("url", "title", "status"), row)) if row else None

    def items_for_ref(self, ref_url: str) -> list[dict]:
        return self._item_rows("WHERE ref_url=?", (ref_url,))

    def item_by_id(self, item_id: int) -> dict | None:
        rows = self._item_rows("WHERE id=?", (item_id,))
        return rows[0] if rows else None

    def _item_rows(self, where: str, params: tuple) -> list[dict]:
        keys = ("id", "source", "kind", "title", "ref_title", "ref_url")
        rows = self.db.execute(
            f"SELECT id, source, kind, title, COALESCE(ref_title, ''), COALESCE(ref_url, '') FROM items {where}", params
        ).fetchall()
        return [dict(zip(keys, r)) for r in rows]

    def purge_rows(self, urls: set[str], item_ids: list[int]) -> dict[str, int]:
        """Delete links and items in ONE transaction; secure_delete zeroes freed pages, VACUUM compacts the file."""
        self.db.execute("PRAGMA secure_delete=ON")
        counts = {"links": 0, "items": 0}
        try:
            with self.db:
                for url in urls:
                    counts["links"] += self.db.execute("DELETE FROM links WHERE url=?", (url,)).rowcount
                for item_id in item_ids:
                    counts["items"] += self.db.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount
        finally:
            self.db.execute("PRAGMA secure_delete=OFF")
        self.db.execute("VACUUM")
        return counts

    def save_items(self, run_id: str, items: list[dict]) -> None:
        self.db.executemany(
            "INSERT INTO items(run_id, source, kind, title, details, owner, due, priority, ref_title, ref_url, raw, "
            "tags, tag_origin, area, area_origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id, it.get("source"), it.get("kind"), it.get("title"), it.get("details"),
                    it.get("owner"), it.get("due"), it.get("priority"), it.get("ref_title"),
                    it.get("ref_url"), json.dumps(it, ensure_ascii=False),
                    *self._tag_columns(it),
                    it.get("area") or FALLBACK_AREA, it.get("area_origin") or "auto",
                )
                for it in items
            ],
        )
        self.db.commit()
