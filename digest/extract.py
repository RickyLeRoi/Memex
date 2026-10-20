from __future__ import annotations

import logging
import re
from datetime import date

from .config import Config
from .llm import LLM, LLMError
from .areas import AreaCatalog, area_rules
from .models import Doc
from .recipes import extract_recipe, recipe_tags
from .state import FALLBACK_AREA
from .tags import Vocabulary, tag_rules

log = logging.getLogger(__name__)

KINDS = ("task", "deadline", "decision", "question", "info", "idea")
PRIORITIES = ("alta", "media", "bassa")

ITEMS_SYSTEM = """Estrai informazioni operative da comunicazioni di lavoro (mail, chat Teams, pagine Notion).
{me_line}{focus_line}Oggi è {today}.

Rispondi SOLO con un oggetto JSON in questo formato:
{{"items": [{{"kind": "task|deadline|decision|question|info|idea", "title": "frase breve e specifica",
  "details": "contesto essenziale: chi, cosa, numeri, riferimenti", "owner": "chi deve agire, 'me' se è l'utente, altrimenti nome o null",
  "due": "YYYY-MM-DD oppure null", "priority": "alta|media|bassa", "tags": ["tag1", "tag2"],
  "area": "nome dell'area", "area_proposal": null, "ref": <numero del documento [#N]>}}]}}

Tipi:
- task: qualcosa che qualcuno deve fare (richieste, promesse, "ci penso io", "mi mandi...")
- deadline: scadenze o date esplicite (riunioni, consegne, rilasci)
- decision: decisioni prese o approvate
- question: domande rivolte all'utente o rimaste senza risposta
- info: fatti utili da ricordare (numeri, nomi, riferimenti, cambi di piano)
- idea: proposte o spunti interessanti

Regole:
- Ignora saluti, convenevoli, notifiche automatiche, newsletter, pubblicità.
- Le righe marcate "(contesto, già letto)" servono solo a capire il resto: non estrarre nulla che venga solo da lì.
- Non inventare nulla. Se non c'è niente di utile restituisci {{"items": []}}.
- Converti le date relative ("domani", "venerdì") usando la data del messaggio.
- Non riportare mai password, token o altri segreti.
- Ogni elemento ha dei tag. {tag_rules}
- Ogni elemento ha un'area. {area_rules}
- Scrivi title e details in {language}."""

LINK_SYSTEM = """Analizza il contenuto di un link salvato dall'utente (articolo, post Instagram/Threads, video).
{me_line}{focus_line}Oggi è {today}.

Rispondi SOLO con un oggetto JSON:
{{"title": "titolo chiaro", "summary": "2-4 frasi sul contenuto", "key_points": ["punti chiave concreti: numeri, nomi, consigli, strumenti"],
  "tags": ["tag1", "tag2"], "actions": ["cosa potrebbe fare l'utente con questa info, solo se sensato"],
  "worth_it": "alta|media|bassa", "is_recipe": false, "area": "nome dell'area", "area_proposal": null}}

is_recipe è true SOLO se il contenuto è una ricetta di cucina (ingredienti e/o procedimento), altrimenti false.
Regole: se c'è una "Nota dell'utente" usala per capire perché l'ha salvato e dai priorità a quell'aspetto.
Ignora testo di interfaccia (login, cookie, menu). Non inventare. Scrivi in {language}.
Ogni contenuto ha dei tag. {tag_rules}
Ogni contenuto ha un'area. {area_rules}"""

HIGHLIGHTS_SYSTEM = """Ricevi una lista di elementi già estratti da mail, chat, note e link.
{me_line}Oggi è {today}.
Scegli le 3-7 cose più importanti per l'utente (priorità a ciò che richiede una sua azione o ha una scadenza vicina).
Rispondi SOLO con JSON: {{"highlights": ["frase breve e concreta", ...]}}. Scrivi in {language}."""


def _fmt(template: str, cfg: Config, vocabulary: Vocabulary | None = None, catalog: AreaCatalog | None = None) -> str:
    ec = cfg.extract
    return template.format(
        me_line=f"L'utente si chiama {ec.me}.\n" if ec.me else "",
        focus_line=f"Interessi dell'utente: {ec.focus}\n" if ec.focus else "",
        today=date.today().isoformat(),
        language=ec.language,
        tag_rules=tag_rules(vocabulary),
        area_rules=area_rules(catalog),
    )


def _segments(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    header = text.split("\n", 1)[0][:200] + " (continua)"
    body_limit = max(limit - len(header) - 1, 500)
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > body_limit:
            lines.append(line[:body_limit])
            line = line[body_limit:]
        lines.append(line)
    segs: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if segs[-1] and size + len(line) + 1 > limit:
            segs.append([header])
            size = len(header)
        segs[-1].append(line)
        size += len(line) + 1
    return ["\n".join(s).strip() for s in segs if "\n".join(s).strip()]


def pack(docs: list[Doc], limit: int) -> list[list[tuple[Doc, str]]]:
    chunks: list[list[tuple[Doc, str]]] = []
    cur: list[tuple[Doc, str]] = []
    size = 0
    for d in docs:
        for seg in _segments(d.text, limit):
            if cur and size + len(seg) > limit:
                chunks.append(cur)
                cur, size = [], 0
            cur.append((d, seg))
            size += len(seg)
    if cur:
        chunks.append(cur)
    return chunks


def _norm_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    kind = str(raw.get("kind") or "info").lower().strip()
    kind = kind if kind in KINDS else "info"
    prio = str(raw.get("priority") or "media").lower().strip()
    prio = {"high": "alta", "medium": "media", "low": "bassa"}.get(prio, prio)
    prio = prio if prio in PRIORITIES else "media"
    due = raw.get("due")
    due = due if isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due.strip()) else None
    owner = raw.get("owner")
    owner = str(owner).strip() if owner not in (None, "", "null") else None
    return {"kind": kind, "title": title, "details": str(raw.get("details") or "").strip(),
            "owner": owner, "due": due, "priority": prio, "ref": raw.get("ref"), "tags": raw.get("tags"),
            "area": raw.get("area"), "area_proposal": raw.get("area_proposal")}


def _is_me(owner: str | None, me: str) -> bool:
    if not owner:
        return False
    o = owner.lower()
    if o in ("me", "io", "utente", "l'utente", "user"):
        return True
    return bool(me) and me.lower() in o


def extract_items(llm: LLM, cfg: Config, docs: list[Doc], vocabulary: Vocabulary | None = None,
                  catalog: AreaCatalog | None = None) -> tuple[list[dict], list[Doc], list[str]]:
    """Return (items, docs_ok, errors). docs_ok = documents analysed without errors (to be marked as seen)."""
    vocabulary = vocabulary if vocabulary is not None else Vocabulary()
    catalog = catalog if catalog is not None else AreaCatalog()
    items: list[dict] = []
    failed: set[str] = set()
    errors: list[str] = []
    chunks = pack(docs, cfg.llm.chunk_chars)
    for n, chunk in enumerate(chunks, 1):
        refs: dict[int, Doc] = {}
        parts = []
        for d, seg in chunk:
            idx = next((k for k, v in refs.items() if v is d), None) or len(refs) + 1
            refs[idx] = d
            parts.append(f"[#{idx}] ({d.source}) {d.title}\n{seg}")
        log.info("Estrazione blocco %d/%d (%d caratteri)", n, len(chunks), sum(len(p) for p in parts))
        try:
            # 20261002 ** RG #tags_vocabulary rebuilt per chunk: tags created in earlier chunks are reusable
            out = llm.chat_json(_fmt(ITEMS_SYSTEM, cfg, vocabulary, catalog), "\n\n---\n\n".join(parts))
        except LLMError as e:
            errors.append(f"blocco {n}/{len(chunks)}: {e}")
            failed.update(d.id for d in refs.values())
            continue
        for raw in out.get("items") or []:
            it = _norm_item(raw)
            if not it:
                continue
            try:
                d = refs.get(int(it.pop("ref")))
            except (TypeError, ValueError):
                d = None
            d = d or (next(iter(refs.values())) if len(refs) == 1 else None)
            it.update(source=d.source if d else "?", ref_title=d.title if d else "",
                      ref_url=d.url if d else "", mine=_is_me(it["owner"], cfg.extract.me))
            it["tags"], it["tag_origin"] = vocabulary.finalize(
                it.get("tags"), source=it["source"], kind=it["kind"])
            it["area"], it["area_origin"], proposal = catalog.finalize(
                it.get("area"), it.pop("area_proposal", None), example=it["title"])
            if proposal:
                it["area_proposal"] = proposal  # collected (and removed) by the caller
            items.append(it)
    ok = [d for d in docs if d.id not in failed]
    return dedupe(items), ok, errors


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it["kind"], re.sub(r"\W+", " ", it["title"].lower()).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _is_true(value: object) -> bool:
    return value is True or str(value).strip().lower() in ("true", "sì", "si", "yes")


def summarize_link(llm: LLM, cfg: Config, doc: Doc, vocabulary: Vocabulary | None = None,
                   catalog: AreaCatalog | None = None) -> dict:
    vocabulary = vocabulary if vocabulary is not None else Vocabulary()
    catalog = catalog if catalog is not None else AreaCatalog()
    platform = doc.meta.get("platform", "")
    jsonld = doc.meta.get("recipe_jsonld")  # 20261002 ++ RG #recipes the page declares a schema.org Recipe
    out = {} if jsonld else llm.chat_json(_fmt(LINK_SYSTEM, cfg, vocabulary, catalog), doc.text)
    if jsonld or _is_true(out.get("is_recipe")):
        recipe = extract_recipe(llm, cfg, doc, jsonld)
        if recipe["ingredients"] or recipe["steps"]:
            tags, tag_origin = recipe_tags(recipe, vocabulary)
            return {
                "url": doc.url, "platform": platform, "note": doc.meta.get("note", ""), "kind": "recipe",
                "title": recipe["title"] or doc.title, "recipe": recipe, "summary": "", "key_points": [],
                "tags": tags, "tag_origin": tag_origin, "actions": [],
                "worth_it": str(out.get("worth_it") or "media").lower(),
                # 20261002 ++ RG #areas a recipe belongs to the recipes area, if the user still has it
                "area": "ricette" if "ricette" in catalog.active else FALLBACK_AREA,
                "area_origin": "model" if "ricette" in catalog.active else "auto",
            }
        if not out:  # JSON-LD present but unusable: fall back to the normal analysis
            out = llm.chat_json(_fmt(LINK_SYSTEM, cfg, vocabulary, catalog), doc.text)
    as_list = lambda v: [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    tags, tag_origin = vocabulary.finalize(
        out.get("tags"), source="link", fallback_extra=(platform,))
    title = str(out.get("title") or doc.title).strip()
    area, area_origin, proposal = catalog.finalize(out.get("area"), out.get("area_proposal"), example=title)
    return {
        "url": doc.url,
        "kind": "link",
        "area": area,
        "area_origin": area_origin,
        **({"area_proposal": proposal} if proposal else {}),
        "platform": doc.meta.get("platform", ""),
        "note": doc.meta.get("note", ""),
        "title": title,
        "summary": str(out.get("summary") or "").strip(),
        "key_points": as_list(out.get("key_points")),
        "tags": tags,
        "tag_origin": tag_origin,
        "actions": as_list(out.get("actions")),
        "worth_it": str(out.get("worth_it") or "media").lower(),
    }


def highlights(llm: LLM, cfg: Config, items: list[dict], links: list[dict]) -> list[str]:
    # 20261002 ** RG #recipes a recipe is neither urgent nor actionable; a small model fills the gap with inventions
    links = [ln for ln in links if ln.get("kind") != "recipe"]
    if not items and not links:
        return []
    lines = []
    for it in items:
        bits = [it["kind"], it["priority"]]
        if it.get("due"):
            bits.append(f"scad. {it['due']}")
        if it.get("mine"):
            bits.append("tocca all'utente")
        lines.append(f"- [{', '.join(bits)}] {it['title']} — {it['details'][:200]}")
    for ln in links:
        lines.append(f"- [link] {ln['title']} — {ln['summary'][:200]}")
    out = llm.chat_json(_fmt(HIGHLIGHTS_SYSTEM, cfg), "\n".join(lines[:300]))
    return [str(h).strip() for h in out.get("highlights") or [] if str(h).strip()]
