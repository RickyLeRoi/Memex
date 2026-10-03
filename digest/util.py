from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Graph sometimes returns 7 fractional digits: Python accepts at most 6
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_str(value: str | datetime | None) -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def default_since(days: int) -> datetime:
    return utcnow() - timedelta(days=days)


def normalize_ws(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_BLOCK_TAGS = ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
               "blockquote", "pre", "section", "article", "header", "footer"]


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    # newline only after block elements, so "Hi <b>Mario</b>" stays on one line
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.append(" | ")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    return normalize_ws(soup.get_text())


_REPLY_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:Original Message|Messaggio originale)|_{8,}|From:\s|Da:\s|Inviato:\s|Sent:\s"
    r"|On .{5,80} wrote:|Il giorno .{5,80} ha scritto:|Il .{5,80} ha scritto:)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_quoted(text: str) -> str:
    m = _REPLY_RE.search(text)
    # cut only when real text comes first (a forwarded mail without a comment stays whole)
    pre = text[: m.start()].strip() if m else ""
    is_forward = bool(pre) and re.search(r"forward|inoltrat", pre.splitlines()[-1], re.IGNORECASE)
    if m and len(pre) >= 2 and not is_forward:
        text = text[: m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return normalize_ws("\n".join(lines))


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[...troncato...]"
