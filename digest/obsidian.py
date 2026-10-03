from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import Config
from .media import MEDIA_NAME
from .sources.documents import DOC_NAME, DOC_SCHEME
from .report import item_line, link_block, sections

PRIO_EMOJI = {"alta": " ⏫", "media": "", "bassa": " 🔽"}


def _safe_name(title: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit].rstrip() or "senza titolo")


def _yaml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _task_line(it: dict) -> str:
    """Formato plugin Tasks: - [ ] titolo 📅 2026-10-03 ⏫"""
    line = item_line(it, checkbox=True)
    if it.get("due"):
        line += f" 📅 {it['due']}"
    return line + PRIO_EMOJI.get(it["priority"], "")


def write_link_note(vault: Path, folder: str, ln: dict, day: str, media_dir: Path | None = None,
                    docs_dir: Path | None = None) -> Path:
    d = vault / folder
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_name(ln['title'])}.md"
    i = 2
    while path.exists() and _yaml_str(ln["url"]) not in path.read_text(encoding="utf-8"):
        path = d / f"{_safe_name(ln['title'])} ({i}).md"
        i += 1
    fm = [
        "---",
        f"url: {_yaml_str(ln['url'])}",
        f"piattaforma: {ln.get('platform', '')}",
        f"salvato: {day}",
        f"rilevanza: {ln.get('worth_it', 'media')}",
        "tags: [" + ", ".join(ln.get("tags") or []) + "]",
        "---",
        "",
    ]
    body = link_block(ln, heading="#")
    embeds: list[str] = []
    image = ln.get("image")
    if media_dir and image and MEDIA_NAME.match(image) and (media_dir / image).is_file():
        (d / "media").mkdir(exist_ok=True)
        shutil.copy2(media_dir / image, d / "media" / image)
        embeds.append(f"![[{image}]]")
    document = str(ln.get("url", "")).removeprefix(DOC_SCHEME)
    if docs_dir and ln.get("url", "").startswith(DOC_SCHEME) and DOC_NAME.match(document) and (docs_dir / document).is_file():
        (d / "media").mkdir(exist_ok=True)
        shutil.copy2(docs_dir / document, d / "media" / document)
        embeds.append(f"![[{document}]]")
    if embeds:
        heading, _, rest = body.partition("\n")
        body = f"{heading}\n\n" + "\n".join(embeds) + f"\n{rest}"
    path.write_text("\n".join(fm) + body + "\n", encoding="utf-8")
    return path


def write_obsidian(result: dict, cfg: Config, excluded_areas: set[str] | frozenset[str] = frozenset()) -> Path:
    oc = cfg.obsidian
    # 20261002 ++ RG #areas areas the user keeps out of the vault: no note, no line, no image, no highlights/stats
    links = [ln for ln in result["links"] if ln.get("area") not in excluded_areas]
    items = [it for it in result["items"] if it.get("area") not in excluded_areas]
    if len(links) != len(result["links"]) or len(items) != len(result["items"]):
        result = {**result, "links": links, "items": items, "highlights": [], "stats": {}}
        if not links and not items and not result["errors"]:
            return Path(os.path.expanduser(oc.vault)) / oc.folder / f"{result['started'].strftime('%Y-%m-%d')}.md"
    vault = Path(os.path.expanduser(oc.vault))
    day = result["started"].strftime("%Y-%m-%d")
    hour = result["started"].strftime("%H:%M")

    link_notes = {}
    for ln in result["links"]:
        p = write_link_note(vault, oc.links_folder, ln, day, cfg.data_path / "media" if oc.copy_media else None,
                            cfg.data_path / "docs" if oc.copy_documents else None)
        link_notes[ln["url"]] = p.stem

    body = [f"## Run delle {hour}", ""]
    if result["stats"]:
        body += ["_" + " · ".join(f"{k}: {v}" for k, v in result["stats"].items()) + "_", ""]
    if result.get("highlights"):
        body += ["### In evidenza", ""] + [f"- {h}" for h in result["highlights"]] + [""]
    for title, its in sections(result["items"]):
        if not its:
            continue
        fmt = _task_line if oc.tasks_format and title in ("Tocca a te", "Scadenze") else item_line
        body += [f"### {title}", ""] + [fmt(i) for i in its] + [""]
    if link_notes:
        body += ["### Link salvati", ""]
        for ln in result["links"]:
            body.append(f"- [[{link_notes[ln['url']]}]] — {ln.get('summary', '')[:160]}")
        body.append("")
    if result["errors"]:
        body += ["### Problemi", ""] + [f"- {e}" for e in result["errors"]] + [""]

    d = vault / oc.folder
    d.mkdir(parents=True, exist_ok=True)
    note = d / f"{day}.md"
    if note.exists():
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(body))
    else:
        fm = ["---", f"data: {day}", "tags: [digest]", "---", "", f"# Digest {day}", ""]
        note.write_text("\n".join(fm + body), encoding="utf-8")
    return note
