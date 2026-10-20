from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import httpx

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import html_to_text, iso_z, local_str, parse_iso, strip_quoted, truncate, utcnow

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    pass


def graph_scopes(cfg: Config) -> list[str]:
    scopes = ["User.Read"]
    if cfg.mail.enabled:
        scopes.append("Mail.Read")
    if cfg.teams.enabled:
        scopes.append("Chat.Read")
        if cfg.teams.channels:
            scopes += ["Team.ReadBasic.All", "Channel.ReadBasic.All", "ChannelMessage.Read.All"]
    return scopes


class GraphAuth:
    """Device code flow + token cache on disk: log in once, refreshes are automatic."""

    def __init__(self, cfg: Config):
        import msal  # lazy import: only needed when mail/teams are enabled

        self.scopes = graph_scopes(cfg)
        self.cache_path: Path = cfg.data_path / "msal_cache.json"
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(
            cfg.graph.client_id,
            authority=f"https://login.microsoftonline.com/{cfg.graph.tenant}",
            token_cache=self.cache,
        )

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.write_text(self.cache.serialize())
            try:
                os.chmod(self.cache_path, 0o600)
            except OSError:
                pass

    def token(self, interactive: bool | None = None) -> str:
        if interactive is None:
            interactive = sys.stdin.isatty()
        result = None
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(self.scopes, account=accounts[0])
        if not result:
            if not interactive:
                raise GraphError("Token Microsoft assente o scaduto: lancia `python -m digest login`")
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise GraphError(f"Device flow fallito: {flow.get('error_description', flow)}")
            print(flow["message"], file=sys.stderr, flush=True)
            result = self.app.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in result:
            raise GraphError(f"Login fallito: {result.get('error_description', result.get('error'))}")
        return result["access_token"]


class GraphClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client | None = None, base: str = GRAPH):
        self.http = http or httpx.Client(timeout=60)
        self.token = token_provider
        self.base = base

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = self.base + url
        for attempt in range(6):
            r = self.http.get(
                url, params=params, headers={"Authorization": f"Bearer {self.token()}", **(headers or {})}
            )
            if r.status_code in (429, 503, 504):
                wait = min(int(r.headers.get("Retry-After", 2 ** attempt)), 60)
                log.info("Graph throttling (%s), attendo %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise GraphError(f"Graph {r.status_code} su {url}: {r.text[:300]}")
            return r.json()
        raise GraphError(f"Troppi retry su {url}")

    def paged(self, url: str, params: dict | None = None, headers: dict | None = None,
              limit: int | None = None) -> Iterator[dict]:
        n = 0
        next_url: str | None = url
        while next_url:
            data = self.get(next_url, params, headers)
            params = None  # nextLink already carries the parameters
            for v in data.get("value", []):
                yield v
                n += 1
                if limit and n >= limit:
                    return
            next_url = data.get("@odata.nextLink")


def _addr(r: dict | None) -> str:
    e = (r or {}).get("emailAddress") or {}
    name, addr = e.get("name", ""), e.get("address", "")
    return f"{name} <{addr}>" if name and addr and name != addr else (addr or name)


def fetch_mail(gc: GraphClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    mc = cfg.mail
    docs: list[Doc] = []
    skipped: list[str] = []
    newest = since
    excl = [p.lower() for p in mc.exclude_senders]
    select = ("id,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
              "body,webLink,importance,hasAttachments")
    for folder in mc.folders:
        params = {
            "$filter": f"receivedDateTime ge {iso_z(since)}",
            "$orderby": "receivedDateTime desc",
            "$top": "50",
            "$select": select,
        }
        headers = {"Prefer": 'outlook.body-content-type="text"'}
        for m in gc.paged(f"/me/mailFolders/{folder}/messages", params, headers, limit=mc.max_messages):
            mid = m.get("internetMessageId") or m["id"]
            received = parse_iso(m.get("receivedDateTime"))
            if received and received > newest:
                newest = received
            if state.is_seen("mail", mid):
                continue
            sender = _addr(m.get("from"))
            if any(p in sender.lower() for p in excl):
                skipped.append(mid)
                continue
            body = (m.get("body") or {}).get("content", "")
            if (m.get("body") or {}).get("contentType") == "html":
                body = html_to_text(body)
            if mc.strip_quoted:
                body = strip_quoted(body)
            header = [
                f"Da: {sender}",
                f"A: {', '.join(_addr(r) for r in m.get('toRecipients', []))}",
            ]
            if m.get("ccRecipients"):
                header.append(f"Cc: {', '.join(_addr(r) for r in m['ccRecipients'])}")
            header += [f"Data: {local_str(m.get('receivedDateTime'))}", f"Oggetto: {m.get('subject') or '(senza oggetto)'}"]
            if m.get("importance") == "high":
                header.append("Importanza: alta")
            if m.get("hasAttachments"):
                header.append("Allegati: sì")
            docs.append(Doc(
                source="mail", id=mid, title=m.get("subject") or "(senza oggetto)",
                text="\n".join(header) + "\n\n" + truncate(body, mc.max_body_chars),
                url=m.get("webLink", ""), timestamp=m.get("receivedDateTime", ""),
            ))
    docs.reverse()
    return FetchResult(docs=docs, cursor=iso_z(newest), skipped_ids=skipped)


def _msg_sender(msg: dict) -> str:
    frm = msg.get("from") or {}
    for k in ("user", "application", "device"):
        if frm.get(k):
            return frm[k].get("displayName") or k
    return "?"


def _msg_text(msg: dict) -> str:
    body = msg.get("body") or {}
    text = body.get("content", "")
    if body.get("contentType") == "html":
        text = html_to_text(text)
    att = [a.get("name") for a in msg.get("attachments") or [] if a.get("name")]
    if att:
        text += f" [allegati: {', '.join(att)}]"
    return text.strip()


def _chat_title(gc: GraphClient, chat: dict, me_id: str) -> str:
    if chat.get("topic"):
        return chat["topic"]
    try:
        members = gc.get(f"/me/chats/{chat['id']}/members").get("value", [])
        names = [m.get("displayName") for m in members if m.get("userId") != me_id and m.get("displayName")]
        if names:
            return ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
    except GraphError as e:
        log.debug("Membri chat non leggibili: %s", e)
    return f"Chat {chat.get('chatType', '')}".strip()


def fetch_teams(gc: GraphClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    tc = cfg.teams
    me = gc.get("/me", {"$select": "id,displayName"})
    docs: list[Doc] = []
    newest = since
    chats = gc.paged(
        "/me/chats",
        {"$expand": "lastMessagePreview", "$top": "50", "$orderby": "lastMessagePreview/createdDateTime desc"},
        limit=tc.max_chats,
    )
    for chat in chats:
        prev = parse_iso(((chat.get("lastMessagePreview") or {}).get("createdDateTime")))
        if prev and prev < since:
            continue
        new_msgs, context = [], []
        try:
            msgs = list(gc.paged(f"/me/chats/{chat['id']}/messages",
                                 {"$top": "50", "$orderby": "lastModifiedDateTime desc"},
                                 limit=tc.max_messages_per_chat))
        except GraphError as e:
            log.warning("Chat %s non leggibile, la salto: %s", chat.get("topic") or chat["id"], e)
            continue
        for msg in msgs:
            if msg.get("messageType") != "message" or msg.get("deletedDateTime"):
                continue
            created = parse_iso(msg.get("createdDateTime"))
            is_new = bool(created and created >= since) and not state.is_seen("teams", msg["id"])
            if is_new:
                new_msgs.append(msg)
                newest = max(newest, created)
            elif created and created < since:
                context.append(msg)
                if len(context) >= tc.context_messages:
                    break
        if not new_msgs:
            continue
        title = _chat_title(gc, chat, me["id"])
        rows = sorted(context + new_msgs, key=lambda m: m.get("createdDateTime", ""))
        new_ids = {m["id"] for m in new_msgs}
        lines = [f"Chat Teams: {title} ({chat.get('chatType', '')})"]
        for m in rows:
            tag = "" if m["id"] in new_ids else "(contesto, già letto) "
            lines.append(f"{tag}[{local_str(m.get('createdDateTime'))}] {_msg_sender(m)}: {_msg_text(m)}")
        docs.append(Doc(
            source="teams", id=chat["id"], title=title, text="\n".join(lines),
            url=chat.get("webUrl", ""), timestamp=max(m["createdDateTime"] for m in new_msgs),
            meta={"msg_ids": sorted(new_ids)},
        ))

    if tc.channels:
        docs += _fetch_channels(gc, cfg, state, since)
    return FetchResult(docs=docs, cursor=iso_z(newest))


def _fetch_channels(gc: GraphClient, cfg: Config, state: State, since: datetime) -> list[Doc]:
    tc = cfg.teams
    allow = {a.lower() for a in tc.channel_allowlist}
    docs: list[Doc] = []
    for team in gc.paged("/me/joinedTeams"):
        for ch in gc.paged(f"/teams/{team['id']}/channels"):
            name = f"{team.get('displayName')}/{ch.get('displayName')}"
            if allow and name.lower() not in allow:
                continue
            lines, ids = [f"Canale Teams: {name}"], []
            try:
                msgs = list(gc.paged(f"/teams/{team['id']}/channels/{ch['id']}/messages",
                                     {"$top": "50", "$expand": "replies"}, limit=tc.max_messages_per_channel))
            except GraphError as e:
                log.warning("Canale %s non leggibile: %s", name, e)
                continue
            for post in sorted(msgs, key=lambda m: m.get("createdDateTime", "")):
                thread = [post] + sorted(post.get("replies") or [], key=lambda m: m.get("createdDateTime", ""))
                fresh = [m for m in thread
                         if (parse_iso(m.get("lastModifiedDateTime") or m.get("createdDateTime")) or since) >= since
                         and m.get("messageType") == "message" and not state.is_seen("teams", m["id"])]
                if not fresh:
                    continue
                subject = post.get("subject") or ""
                lines.append(f"\n## Thread{': ' + subject if subject else ''}")
                for m in thread:
                    if m.get("messageType") != "message" or m.get("deletedDateTime"):
                        continue
                    tag = "" if m in fresh else "(contesto) "
                    lines.append(f"{tag}[{local_str(m.get('createdDateTime'))}] {_msg_sender(m)}: {_msg_text(m)}")
                ids += [m["id"] for m in fresh]
            if ids:
                docs.append(Doc(source="teams", id=f"channel:{ch['id']}", title=name, text="\n".join(lines),
                                url=ch.get("webUrl", ""), timestamp=iso_z(utcnow()), meta={"msg_ids": ids}))
    return docs
