"""PDF documents: text PDFs are read with pypdf, scanned PDFs page by page with the vision model.

Scanned pages are decided PER PAGE (little or no text). The page image is taken from the PDF itself: only JPEG (DCT)
streams are supported because they can be handed to the vision model as they are. Other image encodings would need an
image library (Pillow / PyMuPDF), which is not a dependency of this project: the page is reported as unreadable.
The PDF is only parsed, never executed (no JavaScript, no actions); it is size-capped, page-capped and refused when it
needs a password.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from ..config import Config
from ..llm import LLM
from ..media import sniff_extension, store_image
from ..models import Doc

log = logging.getLogger(__name__)

DOC_SCHEME = "file://"
DOC_NAME = re.compile(r"^[0-9a-f]{40}\.pdf$")
PDF_MAGIC = b"%PDF-"
MIN_TEXT_CHARS = 80  # fewer characters on a page: treat it as a scan
MAX_FORM_DEPTH = 2
PDF_PAGE_PROMPT = ("Questa è una pagina scansionata di un documento. Trascrivi fedelmente tutto il testo visibile "
                   "(intestazioni, date, importi, nomi, tabelle, elenchi) mantenendo l'ordine. Non inventare testo "
                   "che non vedi. Rispondi in italiano.")


class PdfError(Exception):
    pass


@dataclass
class PdfContent:
    text: str
    pages: int
    read_pages: int
    scanned_pages: int
    title: str = ""
    cover: bytes | None = None


def store_pdf(docs_dir: Path, data: bytes, max_bytes: int) -> str:
    if not data:
        raise PdfError("empty file")
    if len(data) > max_bytes:
        raise PdfError(f"file too large (max {max_bytes // 1_000_000} MB)")
    if not data.startswith(PDF_MAGIC):
        raise PdfError("not a PDF file")
    name = f"{hashlib.sha1(data).hexdigest()}.pdf"
    docs_dir.mkdir(parents=True, exist_ok=True)
    target = docs_dir / name
    if not target.exists():
        tmp = target.with_name(name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return name


def _jpeg_streams(resources, depth: int = 0) -> list[bytes]:
    found: list[bytes] = []
    xobjects = (resources or {}).get("/XObject") if resources else None
    if not xobjects:
        return found
    for reference in xobjects.get_object().values():
        obj = reference.get_object()
        subtype = obj.get("/Subtype")
        if subtype == "/Image":
            filters = obj.get("/Filter")
            names = [str(f) for f in (filters if isinstance(filters, list) else [filters])]
            if "/DCTDecode" in names:
                data = obj.get_data()
                if sniff_extension(data) == "jpg":
                    found.append(data)
        elif subtype == "/Form" and depth < MAX_FORM_DEPTH:
            found += _jpeg_streams(obj.get("/Resources"), depth + 1)
    return found


def _page_jpeg(page, max_bytes: int) -> bytes | None:
    try:
        candidates = [d for d in _jpeg_streams(page.get("/Resources")) if len(d) <= max_bytes]
    except Exception as e:  # malformed objects: a bad page must not take the document down
        log.info("PDF page image unreadable: %s", e)
        return None
    return max(candidates, key=len) if candidates else None


def extract_pdf(data: bytes, cfg: Config, llm: LLM | None) -> PdfContent:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as e:
        raise PdfError("installa pypdf per leggere i PDF (pip install '.[pdf]')") from e
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("il PDF è protetto da password: non lo posso leggere")
        total = len(reader.pages)
    except PdfError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as e:
        raise PdfError(f"PDF non leggibile: {e}") from e

    limit = cfg.links.max_pdf_pages
    parts: list[str] = []
    unreadable: list[int] = []
    scanned = 0
    cover: bytes | None = None
    for number, page in enumerate(reader.pages[:limit], 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:
            log.info("PDF page %d text unreadable: %s", number, e)
            text = ""
        if len(text) >= MIN_TEXT_CHARS:
            parts.append(f"[Pagina {number}]\n{text}")
            continue
        jpeg = _page_jpeg(page, cfg.links.max_screenshot_bytes)
        if jpeg is None:
            if text:
                parts.append(f"[Pagina {number}]\n{text}")
            else:
                unreadable.append(number)
            continue
        if llm is None:
            parts.append(f"[Pagina {number}, scansione]\n[dry-run: the vision model was not called]")
        else:
            if not cfg.llm.vision_model:
                raise PdfError("il PDF contiene pagine scansionate: imposta [llm] vision_model (es. gemma3:4b)")
            log.info("PDF: leggo la pagina scansionata %d/%d", number, min(total, limit))
            description = llm.describe_image(jpeg, "image/jpeg", PDF_PAGE_PROMPT, max_tokens=cfg.llm.max_tokens)
            parts.append(f"[Pagina {number}, scansione]\n{description or '[nessun testo letto]'}")
        scanned += 1
        cover = cover or jpeg

    notes = []
    if total > limit:
        notes.append(f"[Documento troncato: lette le prime {limit} pagine su {total}]")
    if unreadable:
        notes.append(f"[Pagine senza contenuto leggibile: {', '.join(map(str, unreadable))}. Se sono scansioni, "
                     "l'immagine non è un JPEG estraibile: servirebbe una libreria di rendering (Pillow o PyMuPDF), "
                     "non inclusa nel progetto]")
    if not parts:
        raise PdfError(notes[-1] if unreadable else "il PDF non contiene testo né pagine leggibili")
    title = ""
    try:
        title = str((reader.metadata or {}).get("/Title") or "").strip()
    except Exception:
        title = ""
    return PdfContent("\n\n".join(parts + notes), total, min(total, limit), scanned, title, cover)


def pdf_doc(cfg: Config, llm: LLM | None, url: str, note: str) -> Doc:
    name = url.removeprefix(DOC_SCHEME)
    if not DOC_NAME.match(name):
        raise PdfError("invalid document reference")
    path = cfg.data_path / "docs" / name
    if not path.is_file():
        raise PdfError("the stored document is missing")
    content = extract_pdf(path.read_bytes(), cfg, llm)
    image = None
    if content.cover and llm is not None and cfg.links.fetch_images:
        image = store_image(cfg.data_path / "media", content.cover, "jpg")
    pages = f"{content.pages}" + (f" (lette {content.read_pages})" if content.read_pages < content.pages else "")
    header = ["Tipo: documento PDF", f"Pagine: {pages}"]
    if note:
        header.append(f"Nota dell'utente: {note}")
    body = content.text if len(content.text) <= cfg.links.max_chars else content.text[:cfg.links.max_chars] + "\n[...troncato...]"
    return Doc(source="link", id=url, title=note or content.title or "Documento PDF", url=url,
               text="\n".join(header) + "\n\n" + body,
               meta={"note": note, "platform": "documento", "method": ["pdf"] + (["vision"] if content.scanned_pages else []),
                     "image": image})

