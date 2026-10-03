from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime

import httpx

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import iso_z, local_str, parse_iso, truncate

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionError(Exception):
    pass


class NotionClient:
    def __init__(self, token: str, http: httpx.Client | None = None, base: str = API):
        self.http = http or httpx.Client(timeout=60)
        self.base = base
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}

    def request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(6):
            r = self.http.request(method, self.base + path, headers=self.headers, **kw)
            if r.status_code in (429, 502, 503):
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code >= 400:
                raise NotionError(f"Notion {r.status_code} su {path}: {r.text[:300]}")
            return r.json()
        raise NotionError(f"Troppi retry su {path}")


def _rich(rt: list | None) -> str:
    return "".join(x.get("plain_text", "") for x in rt or [])


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rich(prop.get("title")) or "(senza titolo)"
    return "(senza titolo)"


def _prop_value(prop: dict) -> str:
    t = prop.get("type")
    v = prop.get(t)
    if v is None:
        return ""
    if t in ("rich_text", "title"):
        return _rich(v)
    if t in ("select", "status"):
        return v.get("name", "")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in v)
    if t == "date":
        return v.get("start", "") + (f" → {v['end']}" if v.get("end") else "")
    if t == "people":
        return ", ".join(p.get("name", "") for p in v if p.get("name"))
    if t == "checkbox":
        return "sì" if v else "no"
    if t in ("number", "url", "email", "phone_number"):
        return str(v)
    if t == "formula":
        return str(v.get(v.get("type"), "") or "")
    return ""


def page_properties(page: dict) -> list[str]:
    out = []
    for name, prop in (page.get("properties") or {}).items():
        if prop.get("type") == "title":
            continue
        val = _prop_value(prop)
        if val:
            out.append(f"{name}: {val}")
    return out


_PREFIX = {
    "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
    "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> ", "callout": "💡 ",
}


def blocks_text(nc: NotionClient, block_id: str, depth: int, max_depth: int, budget: list[int]) -> list[str]:
    lines: list[str] = []
    cursor = None
    indent = "  " * depth
    while budget[0] > 0:
        params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = nc.request("GET", f"/blocks/{block_id}/children", params=params)
        for b in data.get("results", []):
            t = b.get("type", "")
            body = b.get(t) or {}
            if t == "to_do":
                line = f"[{'x' if body.get('checked') else ' '}] {_rich(body.get('rich_text'))}"
            elif t == "code":
                line = f"```\n{_rich(body.get('rich_text'))}\n```"
            elif t == "child_page":
                line = f"[sottopagina] {body.get('title', '')}"
            elif t == "child_database":
                line = f"[database] {body.get('title', '')}"
            elif t in ("bookmark", "embed", "link_preview"):
                line = f"[link] {body.get('url', '')}"
            elif t == "table_row":
                line = " | ".join(_rich(c) for c in body.get("cells", []))
            elif "rich_text" in body:
                line = _PREFIX.get(t, "") + _rich(body.get("rich_text"))
            else:
                line = ""
            if line.strip():
                lines.append(indent + line)
                budget[0] -= len(line)
            if b.get("has_children") and depth < max_depth and t not in ("child_page", "child_database"):
                lines += blocks_text(nc, b["id"], depth + 1, max_depth, budget)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return lines


def fetch_notion(nc: NotionClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    ncfg = cfg.notion
    docs: list[Doc] = []
    newest = since
    cursor = None
    seen_pages = 0
    done = False
    while not done:
        body = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 50,
            **({"start_cursor": cursor} if cursor else {}),
        }
        data = nc.request("POST", "/search", json=body)
        for page in data.get("results", []):
            edited = parse_iso(page.get("last_edited_time"))
            if not edited or edited < since or seen_pages >= ncfg.max_pages:
                done = True
                break
            seen_pages += 1
            newest = max(newest, edited)
            if page.get("archived") or page.get("in_trash"):
                continue
            title = page_title(page)
            try:
                content = blocks_text(nc, page["id"], 0, ncfg.max_depth, [ncfg.max_page_chars])
            except NotionError as e:
                log.warning("Pagina %s non leggibile: %s", title, e)
                continue
            text = "\n".join(
                [f"Pagina Notion: {title}", f"Ultima modifica: {local_str(page.get('last_edited_time'))}"]
                + page_properties(page) + [""] + content
            )
            fp = hashlib.sha1(text.split("\n", 2)[-1].encode()).hexdigest()
            if state.seen_fp("notion", page["id"]) == fp:
                continue  # only metadata changed, content is identical
            docs.append(Doc(source="notion", id=page["id"], title=title,
                            text=truncate(text, ncfg.max_page_chars), url=page.get("url", ""),
                            timestamp=page.get("last_edited_time", ""), meta={"fp": fp}))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return FetchResult(docs=docs, cursor=iso_z(newest))
