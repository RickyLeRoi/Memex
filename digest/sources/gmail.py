"""Gmail over IMAP (stdlib imaplib) with an app password. Read-only: folders are opened with readonly=True."""

from __future__ import annotations

import email
import imaplib
import logging
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import html_to_text, iso_z, local_str, strip_quoted, truncate

log = logging.getLogger(__name__)

IMAP_DATE = "%d-%b-%Y"


class GmailError(Exception):
    pass


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def _body(msg: Message) -> str:
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (html if part.get_content_subtype() == "html" else plain).append(text)
    if plain:
        return "\n".join(plain).strip()
    return html_to_text("\n".join(html))


def _attachments(msg: Message) -> list[str]:
    return [_decode(p.get_filename()) for p in msg.walk() if p.get_content_disposition() == "attachment" and p.get_filename()]


def parse_message(raw: bytes, cfg: Config) -> tuple[str, Doc | None, datetime | None]:
    gc = cfg.gmail
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    name, address = parseaddr(_decode(msg.get("From")))
    sender = f"{name} <{address}>" if name else address
    received: datetime | None = None
    if msg.get("Date"):
        try:
            received = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            received = None
    if any(p in sender.lower() for p in (x.lower() for x in gc.exclude_senders)):
        return message_id, None, received
    body = _body(msg)
    if gc.strip_quoted:
        body = strip_quoted(body)
    subject = _decode(msg.get("Subject")) or "(senza oggetto)"
    header = [f"Da: {sender}", f"A: {_decode(msg.get('To'))}"]
    if msg.get("Cc"):
        header.append(f"Cc: {_decode(msg['Cc'])}")
    header += [f"Data: {local_str(received)}", f"Oggetto: {subject}"]
    files = _attachments(msg)
    if files:
        header.append(f"Allegati: {', '.join(files)}")
    doc = Doc(
        source="gmail", id=message_id or subject, title=subject,
        text="\n".join(header) + "\n\n" + truncate(body, gc.max_body_chars),
        url=f"https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id.strip('<>')}" if message_id else "",
        timestamp=iso_z(received) if received else "",
    )
    return message_id, doc, received


def fetch_gmail(cfg: Config, state: State, since: datetime, connection: imaplib.IMAP4_SSL | None = None) -> FetchResult:
    gc = cfg.gmail
    docs: list[Doc] = []
    skipped: list[str] = []
    newest = since
    conn = connection or imaplib.IMAP4_SSL(gc.host)
    try:
        try:
            conn.login(gc.user, gc.app_password)
        except imaplib.IMAP4.error as e:
            raise GmailError(f"Gmail login failed (is an app password required?): {e}") from e
        for folder in gc.folders:
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != "OK":
                log.warning("Gmail: folder %s not available", folder)
                continue
            status, data = conn.search(None, "SINCE", since.strftime(IMAP_DATE))
            if status != "OK" or not data or not data[0]:
                continue
            for num in reversed(data[0].split()[-gc.max_messages:]):
                status, parts = conn.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                message_id, doc, received = parse_message(parts[0][1], cfg)
                if received and received < since:
                    continue  # IMAP SINCE has day granularity
                if message_id and state.is_seen("gmail", message_id):
                    continue
                if received and received > newest:
                    newest = received
                if doc is None:
                    if message_id:
                        skipped.append(message_id)
                    continue
                docs.append(doc)
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    docs.sort(key=lambda d: d.timestamp)
    return FetchResult(docs=docs, cursor=iso_z(newest), skipped_ids=skipped)
