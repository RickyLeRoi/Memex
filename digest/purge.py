"""Safe deletion of digested information: removes it from every place it lives.

Places: state.sqlite (links, items), links.txt (otherwise the link is re-imported on the next run), report
JSON/Markdown files, latest.md, dry-run dumps, digest.log, and the Obsidian vault (link note + daily-note lines).

Flow: `plan()` computes exactly what would change and a token; `execute()` recomputes the plan, refuses to run if the
token does not match (the user confirmed a different impact), then deletes the DB rows in one transaction and rewrites
files atomically. Every file operation is checked against an allow-list of roots. A final sweep reports leftovers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .media import MEDIA_NAME
from .obsidian import _yaml_str
from .report import render_markdown
from .sources.documents import DOC_NAME, DOC_SCHEME
from .sources.links import URL_RE
from .state import State
from .util import parse_iso, utcnow

DAILY_NOTE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
RUN_HEADING = "## Run delle "
DRYRUN_SEPARATOR = re.compile(r"(\r?\n\r?\n==========\r?\n\r?\n)")
MAX_SCAN_BYTES = 20_000_000
KINDS = ("link", "doc", "item")


class PurgeError(Exception):
    pass


class NotFound(PurgeError):
    pass


class StaleImpact(PurgeError):
    pass


def url_pattern(url: str) -> re.Pattern[str]:
    """Match the URL but not a longer URL that merely starts with it."""
    return re.compile(re.escape(url) + r"(?![A-Za-z0-9_\-/%~?&=#+@]|\.[A-Za-z0-9])")


@dataclass
class Target:
    kind: str
    id: str
    label: str
    urls: set[str] = field(default_factory=set)
    item_ids: list[int] = field(default_factory=list)
    item_keys: set[tuple[str, str, str]] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    stems: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._patterns = [url_pattern(u) for u in self.urls]

    def mentions_url(self, text: str) -> bool:
        return any(p.search(text) for p in self._patterns)

    def line_matches(self, line: str) -> bool:
        if self.mentions_url(line) or any(f"[[{stem}]]" in line for stem in self.stems):
            return True
        return any(f"**{title}**" in line and (not ref or ref in line) for _, title, ref in self.item_keys)

    def entry_is_item(self, entry: dict) -> bool:
        key = (entry.get("kind", ""), entry.get("title", ""), entry.get("ref_url", ""))
        return entry.get("ref_url") in self.urls or key in self.item_keys


@dataclass
class Edit:
    path: Path
    new_text: str | None  # None = delete the file
    detail: str

    @property
    def action(self) -> str:
        return "delete" if self.new_text is None else "edit"


@dataclass
class Plan:
    target: Target
    db: dict[str, int]
    edits: list[Edit]
    token: str


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".purge.tmp")
    tmp.write_bytes(text.encode("utf-8", errors="surrogateescape"))
    os.replace(tmp, path)


def _retry_io(action, attempts: int = 5) -> None:
    """Windows antivirus/indexers briefly hold files open: retry a locked file a few times before giving up."""
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def scrub_lines(text: str, target: Target) -> str:
    return "\n".join(line for line in text.split("\n") if not target.line_matches(line))


def scrub_dryrun(text: str, target: Target) -> str | None:
    parts = DRYRUN_SEPARATOR.split(text)  # [block, separator, block, ...]; files may use CRLF on Windows
    blocks = parts[0::2]
    kept = [b for b in blocks if not target.mentions_url("\n".join(b.split("\n")[:3]))]
    if len(kept) == len(blocks):
        return text
    separator = parts[1] if len(parts) > 1 else "\n\n"
    return separator.join(kept) if kept else None


def scrub_links_file(text: str, target: Target) -> str:
    kept = []
    for line in text.split("\n"):
        match = URL_RE.search(line)
        if match and match.group(0).rstrip(").,;") in target.urls:
            continue
        kept.append(line)
    return "\n".join(kept)


def scrub_daily_note(text: str, target: Target) -> str:
    """Remove matching lines; in a run block that lost something also drop the free-text highlights
    (they cannot be attributed) and any section left empty."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith(RUN_HEADING)]
    if not starts:
        return scrub_lines(text, target)
    out = lines[: starts[0]]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        block = lines[start:end]
        cleaned = [ln for ln in block if not target.line_matches(ln)]
        if len(cleaned) != len(block):
            cleaned = _drop_sections(cleaned, {"### In evidenza"})
            cleaned = _drop_empty_sections(cleaned)
            if not any(ln.startswith("### ") for ln in cleaned):
                continue
        out += cleaned
    return "\n".join(out)


def _sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for ln in lines:
        if ln.startswith("### ") and sections[-1]:
            sections.append([])
        sections[-1].append(ln)
    return sections


def _drop_sections(lines: list[str], headings: set[str]) -> list[str]:
    return [ln for sec in _sections(lines) if sec[0].strip() not in headings for ln in sec]


def _drop_empty_sections(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for sec in _sections(lines):
        if sec[0].startswith("### ") and not any(ln.strip() for ln in sec[1:]):
            continue
        kept += sec
    return kept


class Purger:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.reports = cfg.reports_location
        self.data = cfg.data_path
        vault = os.path.expanduser(cfg.obsidian.vault) if cfg.obsidian.vault else ""
        self.vault = Path(vault) if vault and Path(vault).is_dir() else None

    def resolve(self, kind: str, ident: str) -> Target:
        if kind not in KINDS:
            raise PurgeError(f"unknown kind: {kind}")
        if kind == "link":
            row = self.state.link_row(ident)
            image = self.state.link_image(ident)
            document = ident.removeprefix(DOC_SCHEME)
            # 20261002 ++ RG #documents screenshots and PDFs also produce items (deadlines, tasks) tied to their url
            rows = self.state.items_for_ref(ident)
            return Target(kind, ident, (row or {}).get("title") or ident, urls={ident},
                          images={image} if image else set(),
                          documents={document} if ident.startswith(DOC_SCHEME) and DOC_NAME.match(document) else set(),
                          item_ids=[r["id"] for r in rows],
                          item_keys={(r["kind"], r["title"], r["ref_url"]) for r in rows},
                          sources={r["source"] for r in rows})
        if kind == "doc":
            rows = self.state.items_for_ref(ident)
            label = next((r["ref_title"] for r in rows if r["ref_title"]), ident)
            return Target(kind, ident, label, urls={ident}, item_ids=[r["id"] for r in rows],
                          item_keys={(r["kind"], r["title"], r["ref_url"]) for r in rows},
                          sources={r["source"] for r in rows})
        try:
            item_id = int(ident)
        except ValueError:
            raise NotFound(f"invalid item id: {ident}") from None
        row = self.state.item_by_id(item_id)
        if not row:
            raise NotFound(f"item {item_id} not found")
        return Target(kind, ident, row["title"], item_ids=[item_id],
                      item_keys={(row["kind"], row["title"], row["ref_url"])}, sources={row["source"]})

    def plan(self, kind: str, ident: str) -> Plan:
        target = self.resolve(kind, ident)
        edits: list[Edit] = []
        edits += self._obsidian_link_notes(target)  # first: fills target.stems for the daily notes
        edits += self._images(target, edits)
        edits += self._documents(target, edits)
        edits += self._links_file(target)
        edits += self._reports(target)
        edits += self._dryrun(target)
        edits += self._log(target)
        edits += self._daily_notes(target)
        db = {"links": sum(1 for u in target.urls if self.state.link_row(u)), "items": len(target.item_ids)}
        digest = hashlib.sha1(json.dumps(
            {"k": kind, "i": ident, "db": db, "e": sorted((str(e.path), e.action) for e in edits)}, sort_keys=True
        ).encode()).hexdigest()
        return Plan(target, db, edits, digest)

    def describe(self, plan: Plan) -> dict:
        return {
            "kind": plan.target.kind, "id": plan.target.id, "label": plan.target.label, "db": plan.db,
            "files": [{"path": self._show(e.path), "action": e.action, "detail": e.detail} for e in plan.edits],
            "token": plan.token,
        }

    def _edit_if_changed(self, path: Path, new_text: str | None, detail: str) -> list[Edit]:
        return [Edit(path, new_text, detail)] if new_text != _read(path) else []

    def _links_file(self, t: Target) -> list[Edit]:
        path = self.cfg.links_path
        if not t.urls or not path.is_file():
            return []
        return self._edit_if_changed(path, scrub_links_file(_read(path), t), "removed from the links list")

    def _reports(self, t: Target) -> list[Edit]:
        edits: list[Edit] = []
        rewritten_md: dict[str, str | None] = {}  # old markdown text -> new text (None = report deleted)
        final_md: dict[Path, str] = {}
        touched: set[Path] = set()
        for json_path in sorted(self.reports.glob("digest_*.json")):
            md_path = json_path.with_suffix(".md")
            try:
                data = json.loads(_read(json_path))
            except json.JSONDecodeError:
                continue  # unreadable report: the final sweep will flag it if it mentions the URL
            if not self._scrub_report_data(data, t):
                if md_path.is_file():
                    final_md[md_path] = _read(md_path)
                continue
            touched |= {json_path, md_path}
            if not (data["items"] or data["links"] or data["errors"]):
                # a report emptied by this deletion holds no information any more: remove the shell too
                edits.append(Edit(json_path, None, "report left empty"))
                if md_path.is_file():
                    rewritten_md[_read(md_path)] = None
                    edits.append(Edit(md_path, None, "report left empty"))
                continue
            edits.append(Edit(json_path, json.dumps(data, ensure_ascii=False, indent=2), "report data"))
            if md_path.is_file():
                started = parse_iso(data.get("started")) or utcnow()
                new_md = render_markdown({**data, "started": started})
                rewritten_md[_read(md_path)] = new_md
                final_md[md_path] = new_md
                edits.append(Edit(md_path, new_md, "report text"))
        newest_surviving = final_md[max(final_md)] if final_md else None
        for md_path in sorted(self.reports.glob("*.md")):
            if md_path in touched:
                continue
            old = _read(md_path)
            if old in rewritten_md:  # latest.md mirrors a changed report: follow it, or the newest survivor
                new = rewritten_md[old] if rewritten_md[old] is not None else newest_surviving
            else:
                new = scrub_lines(old, t)
            edits += self._edit_if_changed(md_path, new, "report text")
        return edits

    @staticmethod
    def _scrub_report_data(data: dict, t: Target) -> bool:
        items = data.get("items") or []
        links = data.get("links") or []
        errors = data.get("errors") or []
        kept_items = [e for e in items if not t.entry_is_item(e)]
        kept_links = [e for e in links if e.get("url") not in t.urls]
        kept_errors = [e for e in errors if not t.mentions_url(str(e))]
        removed_items, removed_links = len(items) - len(kept_items), len(links) - len(kept_links)
        if not (removed_items or removed_links or len(kept_errors) != len(errors)):
            return False
        data.update(items=kept_items, links=kept_links, errors=kept_errors, highlights=[])
        stats = data.get("stats") or {}
        if removed_links and "link" in stats:
            stats["link"] = max(0, stats["link"] - 1)
        if t.kind == "doc":
            for src in {e.get("source") for e in items if t.entry_is_item(e)} & set(stats):
                stats[src] = max(0, stats[src] - 1)
        return True

    def _dryrun(self, t: Target) -> list[Edit]:
        edits: list[Edit] = []
        if not t.urls:
            return edits
        for path in sorted((self.reports / "dryrun").glob("*.txt")):
            edits += self._edit_if_changed(path, scrub_dryrun(_read(path), t), "dry-run dump")
        return edits

    def _log(self, t: Target) -> list[Edit]:
        path = self.data / "digest.log"
        if not t.urls or not path.is_file():
            return []
        return self._edit_if_changed(path, scrub_lines(_read(path), t), "log lines")

    def _obsidian_link_notes(self, t: Target) -> list[Edit]:
        if not self.vault or not t.urls:
            return []
        edits: list[Edit] = []
        for path in sorted((self.vault / self.cfg.obsidian.links_folder).glob("*.md")):
            text = _read(path).replace("\r\n", "\n")  # files written on Windows use CRLF
            head = text[:2000]
            is_ours = text.startswith("---\nurl: ") and "\npiattaforma:" in head and "\nsalvato:" in head
            if is_ours and any(f"url: {_yaml_str(u)}" in text for u in t.urls):
                t.stems.add(path.stem)
                edits.append(Edit(path, None, "Obsidian link note"))
        return edits

    def _images(self, t: Target, planned: list[Edit]) -> list[Edit]:
        edits: list[Edit] = []
        deleted_notes = {e.path for e in planned if e.new_text is None}
        for name in sorted(t.images):
            if not MEDIA_NAME.match(name):
                continue  # never build a path from a value that is not one of our content-addressed names
            stored = self.data / "media" / name
            if stored.is_file() and self.state.image_users(name, t.urls) == 0:
                edits.append(Edit(stored, None, "stored image"))
            if not self.vault:
                continue
            links_dir = self.vault / self.cfg.obsidian.links_folder
            copy = links_dir / "media" / name
            still_embedded = any(
                f"![[{name}]]" in _read(note) for note in links_dir.glob("*.md") if note not in deleted_notes
            )
            if copy.is_file() and not still_embedded:
                edits.append(Edit(copy, None, "image copy in the vault"))
        return edits

    def _documents(self, t: Target, planned: list[Edit]) -> list[Edit]:
        edits: list[Edit] = []
        deleted_notes = {e.path for e in planned if e.new_text is None}
        for name in sorted(t.documents):
            stored = self.data / "docs" / name
            if stored.is_file():
                edits.append(Edit(stored, None, "stored document"))
            if not self.vault:
                continue
            links_dir = self.vault / self.cfg.obsidian.links_folder
            copy = links_dir / "media" / name
            still_embedded = any(
                f"![[{name}]]" in _read(note) for note in links_dir.glob("*.md") if note not in deleted_notes
            )
            if copy.is_file() and not still_embedded:
                edits.append(Edit(copy, None, "document copy in the vault"))
        return edits

    def _daily_notes(self, t: Target) -> list[Edit]:
        if not self.vault:
            return []
        edits: list[Edit] = []
        for path in sorted((self.vault / self.cfg.obsidian.folder).glob("*.md")):
            if not DAILY_NOTE.match(path.name):
                continue
            edits += self._edit_if_changed(path, scrub_daily_note(_read(path), t), "Obsidian daily note")
        return edits

    def vault_cleanup(self, area: str, apply: bool = True) -> dict:
        if not self.vault:
            return {"edited": [], "errors": []}
        urls, images, documents = set(), set(), set()
        for url, raw in self.state.db.execute("SELECT url, analysis FROM links WHERE analysis IS NOT NULL").fetchall():
            if json.loads(raw).get("area") == area:
                urls.add(url)
                if (image := self.state.link_image(url)):
                    images.add(image)
                if url.startswith(DOC_SCHEME) and DOC_NAME.match(url.removeprefix(DOC_SCHEME)):
                    documents.add(url.removeprefix(DOC_SCHEME))
        rows = self.state.db.execute(
            "SELECT kind, title, COALESCE(ref_url, '') FROM items WHERE area=?", (area,)).fetchall()
        target = Target("area", area, area, urls=urls, item_keys=set(map(tuple, rows)), images=images,
                        documents=documents)
        edits = self._obsidian_link_notes(target)
        edits += self._images(target, edits) + self._documents(target, edits) + self._daily_notes(target)
        vault_root = self.vault.resolve()
        edits = [e for e in edits if e.path.resolve().is_relative_to(vault_root)]  # never the stored originals
        edited, errors = [], []
        for edit in edits if apply else []:
            try:
                self._apply(edit)
                edited.append(self._show(edit.path))
            except (OSError, PurgeError) as e:
                errors.append(f"{self._show(edit.path)}: {e}")
        return {"edited": edited if apply else [self._show(e.path) for e in edits], "errors": errors}

    def execute(self, kind: str, ident: str, token: str) -> dict:
        plan = self.plan(kind, ident)
        if plan.token != token:
            raise StaleImpact("the impact changed since it was shown: review it again")
        errors: list[str] = []
        done: list[str] = []
        # 20261002 ** RG #safe_delete links.txt goes FIRST: if it cannot be cleaned (a transient lock on Windows) the
        # database is left alone, because a link left in that file would come back at the next run
        links_file = self.cfg.links_path.resolve()
        critical = [e for e in plan.edits if e.path.resolve() == links_file]
        for edit in critical:
            try:
                self._apply(edit)
                done.append(self._show(edit.path))
            except (OSError, PurgeError) as e:
                return {"ok": False, "db": {"links": 0, "items": 0}, "edited": done, "leftovers": [],
                        "errors": [f"{self._show(edit.path)}: {e} (nothing was deleted: try again)"]}
        counts = self.state.purge_rows(plan.target.urls, plan.target.item_ids)
        for edit in (e for e in plan.edits if e not in critical):
            try:
                self._apply(edit)
                done.append(self._show(edit.path))
            except (OSError, PurgeError) as e:
                errors.append(f"{self._show(edit.path)}: {e}")
        leftovers = self._sweep(plan.target)
        blocking = [x for x in leftovers if x["blocking"]]
        return {"ok": not errors and not blocking, "db": counts, "edited": done, "errors": errors,
                "leftovers": leftovers}

    def _allowed_roots(self) -> list[Path]:
        roots = [self.reports, self.data, self.cfg.base_dir]
        if self.vault:
            roots.append(self.vault)
        return [r.resolve() for r in roots]

    def _apply(self, edit: Edit) -> None:
        resolved = edit.path.resolve()
        if resolved != self.cfg.links_path.resolve() and not any(resolved.is_relative_to(r) for r in self._allowed_roots()):
            raise PurgeError("path outside the allowed folders")
        if edit.new_text is None:
            _retry_io(edit.path.unlink)
        else:
            _retry_io(lambda: _write_atomic(edit.path, edit.new_text))

    def _sweep(self, t: Target) -> list[dict]:
        """Look for the URL in every place we know. Blocking = places that must be clean."""
        if not t.urls:
            return []
        spots: list[tuple[Path, bool]] = [(self.cfg.links_path, True), (self.data / "digest.log", True)]
        spots += [(p, False) for p in self.reports.rglob("*") if p.is_file()]
        if self.vault:
            links_dir = self.vault / self.cfg.obsidian.links_folder
            spots += [(p, True) for p in links_dir.glob("*.md")]
            spots += [(p, False) for p in (self.vault / self.cfg.obsidian.folder).glob("*.md")]
        found = []
        for path, blocking in spots:
            if path.is_file() and path.stat().st_size <= MAX_SCAN_BYTES and t.mentions_url(_read(path)):
                found.append({"path": self._show(path), "blocking": blocking})
        for name in sorted(t.images):
            if MEDIA_NAME.match(name) and (self.data / "media" / name).is_file() and not self.state.image_users(name):
                found.append({"path": f"data:media/{name}", "blocking": True})
        for name in sorted(t.documents):
            if (self.data / "docs" / name).is_file():
                found.append({"path": f"data:docs/{name}", "blocking": True})
        stale_db = [u for u in t.urls if self.state.link_row(u)]
        found += [{"path": "state.sqlite (links)", "blocking": True} for _ in stale_db]
        return found

    def _show(self, path: Path) -> str:
        for label, root in (("vault", self.vault), ("data", self.data), ("", self.cfg.base_dir)):
            if root and path.resolve().is_relative_to(root.resolve()):
                rel = path.resolve().relative_to(root.resolve()).as_posix()
                return f"{label}:{rel}" if label else rel
        return path.name
