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

FIELDS = ["summary", "status", "assignee", "reporter", "priority", "issuetype", "duedate", "labels",
          "description", "updated", "created", "comment", "project"]


class JiraError(Exception):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=60, auth=(email, api_token))
        self.headers = {"Accept": "application/json"}

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(6):
            r = self.http.get(self.base + path, params=params, headers=self.headers)
            if r.status_code in (429, 503):
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code in (401, 403):
                raise JiraError(f"Jira {r.status_code}: check email/api_token and permissions")
            if r.status_code >= 400:
                raise JiraError(f"Jira {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise JiraError(f"Too many retries on {path}")


def adf_to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return (node.get("attrs") or {}).get("text", "@user")
    if kind in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url", "")
    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("paragraph", "heading", "codeBlock", "blockquote", "tableRow"):
        return inner + "\n"
    return inner


def build_jql(cfg: Config, since: datetime) -> str:
    jc = cfg.jira
    clauses = [f'updated >= "{since.astimezone().strftime("%Y-%m-%d %H:%M")}"']
    if jc.projects:
        clauses.append("project in (" + ", ".join(f'"{p}"' for p in jc.projects) + ")")
    if jc.jql.strip():
        clauses.append(f"({jc.jql.strip()})")
    return " AND ".join(clauses) + " ORDER BY updated ASC"


def _person(field: dict | None) -> str:
    return (field or {}).get("displayName", "") or "nessuno"


def issue_text(issue: dict, cfg: Config) -> str:
    jc = cfg.jira
    f = issue["fields"]
    lines = [
        f"Ticket Jira: {issue['key']} — {f.get('summary', '')}",
        f"Tipo: {(f.get('issuetype') or {}).get('name', '')} · Stato: {(f.get('status') or {}).get('name', '')}"
        f" · Priorità: {(f.get('priority') or {}).get('name', '')}",
        f"Assegnato a: {_person(f.get('assignee'))} · Riporter: {_person(f.get('reporter'))}",
    ]
    if f.get("duedate"):
        lines.append(f"Scadenza: {f['duedate']}")
    if f.get("labels"):
        lines.append(f"Etichette: {', '.join(f['labels'])}")
    lines.append(f"Ultima modifica: {local_str(f.get('updated'))}")
    description = adf_to_text(f.get("description")).strip()
    if description:
        lines += ["", "Descrizione:", truncate(description, jc.max_description_chars)]
    comments = ((f.get("comment") or {}).get("comments") or [])[-jc.max_comments:]
    if comments:
        lines += ["", "Commenti:"]
        for c in comments:
            lines.append(f"[{local_str(c.get('created'))}] {_person(c.get('author'))}: {adf_to_text(c.get('body')).strip()}")
    return "\n".join(lines)


def fetch_jira(client: JiraClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    jc = cfg.jira
    docs: list[Doc] = []
    newest = since
    token = ""
    while len(docs) < jc.max_issues:
        params = {"jql": build_jql(cfg, since), "fields": ",".join(FIELDS), "maxResults": min(50, jc.max_issues),
                  **({"nextPageToken": token} if token else {})}
        data = client.get("/rest/api/3/search/jql", params)
        for issue in data.get("issues", []):
            text = issue_text(issue, cfg)
            fingerprint = hashlib.sha1(text.encode()).hexdigest()
            updated = parse_iso(issue["fields"].get("updated"))
            if updated and updated > newest:
                newest = updated
            if state.seen_fp("jira", issue["key"]) == fingerprint:
                continue
            docs.append(Doc(
                source="jira", id=issue["key"], title=f"{issue['key']} {issue['fields'].get('summary', '')}",
                text=text, url=f"{client.base}/browse/{issue['key']}",
                timestamp=issue["fields"].get("updated", ""), meta={"fp": fingerprint},
            ))
        token = data.get("nextPageToken") or ""
        if data.get("isLast", True) or not token:
            break
    return FetchResult(docs=docs[: jc.max_issues], cursor=iso_z(newest))
