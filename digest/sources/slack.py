"""Slack channels and DMs via the Web API. Read-only scopes: *:history and *:read, plus users:read."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import iso_z, local_str, truncate

log = logging.getLogger(__name__)

API = "https://slack.com/api"
MAX_BODY_CHARS = 12000
_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
_CHANNEL_REF = re.compile(r"<#[CG][A-Z0-9]+\|([^>]+)>")
_SKIPPED_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name"}


class SlackError(Exception):
    pass


class SlackClient:
    def __init__(self, token: str, http: httpx.Client | None = None, base: str = API):
        self.http = http or httpx.Client(timeout=60)
        self.base = base
        self.headers = {"Authorization": f"Bearer {token}"}
        self._users: dict[str, str] = {}

    def call(self, method: str, **params) -> dict:
        for attempt in range(6):
            r = self.http.get(f"{self.base}/{method}", headers=self.headers, params=params)
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code >= 400:
                raise SlackError(f"Slack HTTP {r.status_code} on {method}")
            data = r.json()
            if not data.get("ok"):
                raise SlackError(f"Slack {method}: {data.get('error', 'unknown error')}")
            return data
        raise SlackError(f"Too many retries on {method}")

    def paged(self, method: str, key: str, **params):
        cursor = ""
        while True:
            data = self.call(method, **params, **({"cursor": cursor} if cursor else {}))
            yield from data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                return

    def user_name(self, user_id: str) -> str:
        if user_id not in self._users:
            try:
                profile = self.call("users.info", user=user_id)["user"]
                self._users[user_id] = profile.get("real_name") or profile.get("name") or user_id
            except SlackError:
                self._users[user_id] = user_id
        return self._users[user_id]


def resolve_channels(client: SlackClient, wanted: list[str]) -> list[dict]:
    if not wanted:
        return []
    by_name: dict[str, dict] = {}
    for conv in client.paged("conversations.list", "channels", types="public_channel,private_channel,im,mpim",
                             exclude_archived="true", limit=200):
        label = conv.get("name") or (f"@{client.user_name(conv['user'])}" if conv.get("user") else conv["id"])
        by_name[label.lower()] = {"id": conv["id"], "name": label}
        by_name[conv["id"].lower()] = by_name[label.lower()]
    found, missing = [], []
    for entry in wanted:
        key = entry.strip().lstrip("#").lower()
        (found if key in by_name else missing).append(by_name.get(key) or entry)
    if missing:
        log.warning("Slack: channels not found or not accessible: %s", ", ".join(missing))
    return found


def clean_text(client: SlackClient, text: str) -> str:
    text = _MENTION.sub(lambda m: "@" + client.user_name(m.group(1)), text)
    text = _CHANNEL_REF.sub(r"#\1", text)
    text = _LINK.sub(lambda m: m.group(2) and f"{m.group(2)} ({m.group(1)})" or m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def _ts_to_iso(ts: str) -> str:
    return iso_z(datetime.fromtimestamp(float(ts), tz=timezone.utc))


def _format_message(client: SlackClient, msg: dict, indent: str = "") -> str:
    who = client.user_name(msg["user"]) if msg.get("user") else msg.get("username") or "bot"
    body = clean_text(client, msg.get("text", ""))
    files = [f.get("name") or f.get("title") for f in msg.get("files", []) if f.get("name") or f.get("title")]
    suffix = f" [allegati: {', '.join(files)}]" if files else ""
    return f"{indent}[{local_str(_ts_to_iso(msg['ts']))}] {who}: {body}{suffix}"


def fetch_slack(client: SlackClient, cfg: Config, state: State, since: datetime,
                source: str = "slack") -> FetchResult:
    sc = cfg.slack
    wanted = sc.ticket_channels if source == "slack_tickets" else sc.channels
    docs: list[Doc] = []
    newest = since
    for conv in resolve_channels(client, wanted):
        messages = list(client.paged(
            "conversations.history", "messages", channel=conv["id"], oldest=f"{since.timestamp():.6f}", limit=200,
        ))[: sc.max_messages_per_channel]
        fresh = [m for m in messages
                 if m.get("subtype") not in _SKIPPED_SUBTYPES and m.get("text")
                 and not state.is_seen(source, f"{conv['id']}:{m['ts']}")]
        if not fresh:
            continue
        fresh.sort(key=lambda m: float(m["ts"]))
        lines = [f"Canale Slack: {conv['name']}", ""]
        ids: list[str] = []
        for msg in fresh:
            lines.append(_format_message(client, msg))
            ids.append(f"{conv['id']}:{msg['ts']}")
            if sc.include_threads and msg.get("reply_count"):
                for reply in _thread_replies(client, conv["id"], msg["ts"]):
                    if reply["ts"] != msg["ts"]:
                        lines.append(_format_message(client, reply, indent="    ↳ "))
            newest = max(newest, datetime.fromtimestamp(float(msg["ts"]), tz=timezone.utc))
        permalink = _permalink(client, conv["id"], fresh[-1]["ts"])
        docs.append(Doc(
            source=source, id=conv["id"], title=conv["name"], text=truncate("\n".join(lines), MAX_BODY_CHARS),
            url=permalink, timestamp=_ts_to_iso(fresh[-1]["ts"]), meta={"msg_ids": ids},
        ))
    return FetchResult(docs=docs, cursor=iso_z(newest))


def _thread_replies(client: SlackClient, channel: str, ts: str) -> list[dict]:
    try:
        return list(client.paged("conversations.replies", "messages", channel=channel, ts=ts, limit=100))
    except SlackError as e:
        log.warning("Slack thread %s unreadable: %s", ts, e)
        return []


def _permalink(client: SlackClient, channel: str, ts: str) -> str:
    try:
        return client.call("chat.getPermalink", channel=channel, message_ts=ts).get("permalink", "")
    except SlackError:
        return ""
