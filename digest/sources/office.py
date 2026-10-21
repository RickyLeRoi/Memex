"""Plain text out of Office Open XML files (docx, pptx, xlsx) using only the standard library."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from io import BytesIO
from zipfile import BadZipFile, ZipFile

MAX_PART_BYTES = 20_000_000
SLIDE_PART = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
SHEET_PART = re.compile(r"^xl/worksheets/sheet(\d+)\.xml$")


class OfficeError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_part(archive: ZipFile, name: str) -> ET.Element:
    if archive.getinfo(name).file_size > MAX_PART_BYTES:
        raise OfficeError(f"{name} troppo grande")
    return ET.fromstring(archive.read(name))


def _paragraphs(root: ET.Element) -> list[str]:
    lines = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "p":
            continue
        text = "".join(t.text or "" for t in paragraph.iter() if _local(t.tag) == "t")
        if text.strip():
            lines.append(text)
    return lines


def _docx(archive: ZipFile) -> str:
    return "\n".join(_paragraphs(_read_part(archive, "word/document.xml")))


def _pptx(archive: ZipFile) -> str:
    slides = sorted((int(m.group(1)), n) for n in archive.namelist() if (m := SLIDE_PART.match(n)))
    return "\n\n".join(f"[Slide {number}]\n" + "\n".join(_paragraphs(_read_part(archive, name)))
                       for number, name in slides)


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_part(archive, "xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in si.iter() if _local(t.tag) == "t")
            for si in root if _local(si.tag) == "si"]


def _xlsx(archive: ZipFile) -> str:
    strings = _shared_strings(archive)
    sheets = sorted((int(m.group(1)), n) for n in archive.namelist() if (m := SHEET_PART.match(n)))
    out = []
    for number, name in sheets:
        rows = []
        for row in _read_part(archive, name).iter():
            if _local(row.tag) != "row":
                continue
            cells = []
            for cell in row:
                value = next((v.text or "" for v in cell if _local(v.tag) in ("v", "is")), "")
                if cell.get("t") == "s" and value.isdigit() and int(value) < len(strings):
                    value = strings[int(value)]
                cells.append(value)
            if any(cells):
                rows.append(" | ".join(cells))
        out.append(f"[Foglio {number}]\n" + "\n".join(rows))
    return "\n\n".join(out)


READERS = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx}


def extract_office(data: bytes, extension: str) -> str:
    try:
        with ZipFile(BytesIO(data)) as archive:
            return READERS[extension](archive).strip()
    except (BadZipFile, KeyError, ET.ParseError) as e:
        raise OfficeError(f"file {extension} non leggibile: {e}") from e
