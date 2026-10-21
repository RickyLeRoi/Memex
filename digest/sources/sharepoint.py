"""SharePoint / OneDrive files the user lists in sharepoint.txt, read through Microsoft Graph (same login as Outlook)."""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import PurePosixPath

from ..config import Config
from ..models import Doc, FetchResult
from ..state import State
from ..util import local_str, truncate
from .documents import extract_pdf
from .links import read_links_file
from .msgraph import GraphClient, GraphError
from .office import READERS, extract_office

log = logging.getLogger(__name__)

PLAIN_TEXT = {"txt", "md", "csv"}
ITEM_FIELDS = "id,name,size,eTag,lastModifiedDateTime,webUrl"


def share_id(url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def file_text(name: str, data: bytes, cfg: Config) -> str:
    extension = PurePosixPath(name).suffix.lower().lstrip(".")
    if extension == "pdf":
        return extract_pdf(data, cfg, None).text
    if extension in READERS:
        return extract_office(data, extension)
    if extension in PLAIN_TEXT:
        return data.decode("utf-8", errors="replace")
    raise GraphError(f"formato .{extension or '?'} non supportato (pdf, docx, pptx, xlsx, txt, md, csv)")


def fetch_sharepoint(gc: GraphClient, cfg: Config, state: State, since: datetime) -> FetchResult:
    sc = cfg.sharepoint
    docs: list[Doc] = []
    failures: list[str] = []
    for url, note in read_links_file(cfg.sharepoint_path)[: sc.max_files]:
        try:
            item = gc.get(f"/shares/{share_id(url)}/driveItem", {"$select": ITEM_FIELDS})
            if state.seen_fp("sharepoint", item["id"]) == item.get("eTag"):
                continue
            if item.get("size", 0) > sc.max_file_bytes:
                raise GraphError(f"file troppo grande (max {sc.max_file_bytes // 1_000_000} MB)")
            body = file_text(item["name"], gc.get_bytes(f"/shares/{share_id(url)}/driveItem/content"), cfg).strip()
            if not body:
                raise GraphError("nessun testo estraibile")
        except Exception as e:  # one unreadable file must not hide the others
            log.warning("SharePoint %s non leggibile: %s", url, e)
            failures.append(f"{url}: {e}")
            continue
        header = [f"Documento SharePoint: {item['name']}", f"Ultima modifica: {local_str(item.get('lastModifiedDateTime'))}"]
        if note:
            header.append(f"Nota dell'utente: {note}")
        docs.append(Doc(source="sharepoint", id=item["id"], title=item["name"],
                        text="\n".join(header) + "\n\n" + truncate(body, sc.max_chars),
                        url=item.get("webUrl", url), timestamp=item.get("lastModifiedDateTime", ""),
                        meta={"fp": item.get("eTag")}))
    if failures and not docs:
        raise GraphError("; ".join(failures))
    return FetchResult(docs=docs)
