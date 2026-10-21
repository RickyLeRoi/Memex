from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime

import httpx

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import html_to_text, iso_z, local_str, parse_iso, truncate

log = logging.getLogger(__name__)

PAGE_SIZE = 25


class ConfluenceError(Exception):
    pass


class ConfluenceClient:
    def __init__(self, base_url: str, email: str, api_token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/").removesuffix("/wiki")
        self.http = http or httpx.Client(timeout=60, auth=(email, api_token))
        self.headers = {"Accept": "application/json"}

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(6):
            r = self.http.get(self.base + path, params=params, headers=self.headers)
            if r.status_code in (429, 503):
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code in (401, 403):
                raise ConfluenceError(f"Confluence {r.status_code}: check email/api_token and permissions")
            if r.status_code >= 400:
                raise ConfluenceError(f"Confluence {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise ConfluenceError(f"Too many retries on {path}")


def build_cql(cfg: Config, since: datetime) -> str:
    cc = cfg.confluence
    clauses = ["type = page", f'lastmodified >= "{since.astimezone().strftime("%Y-%m-%d %H:%M")}"']
    if cc.spaces:
        clauses.append("space in (" + ", ".join(f'"{s}"' for s in cc.spaces) + ")")
    if cc.cql.strip():
        clauses.append(f"({cc.cql.strip()})")
    return " AND ".join(clauses) + " ORDER BY lastmodified ASC"


def page_text(page: dict, cfg: Config) -> str:
    version = page.get("version") or {}
    lines = [
        f"Pagina Confluence: {page.get('title', '')}",
        f"Spazio: {(page.get('space') or {}).get('name', '')}",
        f"Ultima modifica: {local_str(version.get('when'))} da {(version.get('by') or {}).get('displayName', '?')}",
    ]
    body = html_to_text(((page.get("body") or {}).get("storage") or {}).get("value", "")).strip()
    if body:
        lines += ["", truncate(body, cfg.confluence.max_page_chars)]
    return "\n".join(lines)


def fetch_confluence(client: ConfluenceClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    cc = cfg.confluence
    docs: list[Doc] = []
    newest = since
    start = 0
    while len(docs) < cc.max_pages:
        data = client.get("/wiki/rest/api/content/search", {
            "cql": build_cql(cfg, since), "expand": "body.storage,version,space",
            "limit": PAGE_SIZE, "start": start,
        })
        results = data.get("results", [])
        for page in results:
            text = page_text(page, cfg)
            storage = ((page.get("body") or {}).get("storage") or {}).get("value", "")
            fingerprint = hashlib.sha1(f"{page.get('title', '')}\n{storage}".encode()).hexdigest()
            modified = parse_iso((page.get("version") or {}).get("when"))
            if modified and modified > newest:
                newest = modified
            if state.seen_fp("confluence", page["id"]) == fingerprint:
                continue
            docs.append(Doc(
                source="confluence", id=page["id"], title=page.get("title", ""), text=text,
                url=f"{client.base}/wiki{(page.get('_links') or {}).get('webui', '')}",
                timestamp=(page.get("version") or {}).get("when", ""), meta={"fp": fingerprint},
            ))
        start += len(results)
        if not results or not (data.get("_links") or {}).get("next"):
            break
    return FetchResult(docs=docs[: cc.max_pages], cursor=iso_z(newest))
