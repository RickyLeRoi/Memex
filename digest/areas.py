"""Areas (macro-topics): a closed list chosen by the user. The model picks ONE area per piece of information and may
PROPOSE a new one, but a proposal never becomes an area until the user approves it. Areas group, colour and filter;
they never create links in the graph."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .config import Config
from .llm import LLM, LLMError
from .state import DEFAULT_AREAS, FALLBACK_AREA, State
from .tags import _variants, canonical

log = logging.getLogger(__name__)

MAX_PROPOSAL_LEN = 30
MAX_PROPOSAL_WORDS = 3
RECLASSIFY_BATCH = 10
ORIGIN_MODEL, ORIGIN_AUTO, ORIGIN_MANUAL = "model", "auto", "manual"

RECLASSIFY_SYSTEM = """Hai una lista di voci già archiviate. L'utente ha appena approvato una NUOVA area: «{label}» ({description}).
Indica SOLO le voci che appartengono chiaramente a questa nuova area. Non spostare le altre.
Aree esistenti, per contesto: {others}.
Rispondi SOLO con JSON: {{"moves": [<numero della voce>, ...]}}. Se nessuna appartiene, {{"moves": []}}."""


def _default_rows() -> list[dict]:
    return [{"name": n, "label": label, "description": d, "color": c, "status": "active", "system": bool(s)}
            for n, label, d, c, s in DEFAULT_AREAS]


class AreaCatalog:

    def __init__(self, areas: list[dict] | None = None):
        rows = areas if areas is not None else _default_rows()
        self.active = {a["name"]: a for a in rows if a["status"] == "active"}
        self.rejected = {a["name"] for a in rows if a["status"] == "rejected"}
        self.everything = {a["name"] for a in rows}
        self._by_label = {canonical(a["label"]): a["name"] for a in self.active.values()}

    @classmethod
    def from_state(cls, state: State) -> "AreaCatalog":
        return cls(state.list_areas())

    def _match(self, raw: object, names: set[str]) -> str | None:
        name = canonical(raw)
        if not name:
            return None
        if name in names:
            return name
        return next((v for v in _variants(name) if v in names), None)

    def resolve(self, raw: object) -> str | None:
        matched = self._match(raw, set(self.active))
        return matched or self._by_label.get(canonical(raw))

    def prompt_block(self) -> str:
        return "\n".join(f"- {a['name']}: {a['description']}" for a in self.active.values())

    def finalize(self, raw_area: object, raw_proposal: object, example: str) -> tuple[str, str, dict | None]:
        """Return (area, origin, proposal). Never empty: unknown or missing means the fallback area."""
        area = self.resolve(raw_area)
        if area and area != FALLBACK_AREA:
            return area, ORIGIN_MODEL, None
        wanted = raw_proposal.get("name") if isinstance(raw_proposal, dict) else None
        why = str(raw_proposal.get("why") or "") if isinstance(raw_proposal, dict) else ""
        if wanted is None and area is None and raw_area:  # the model wrote an area name that does not exist
            wanted = raw_area
        return FALLBACK_AREA, (ORIGIN_MODEL if area else ORIGIN_AUTO), self._proposal(wanted, why, example)

    def _proposal(self, wanted: object, why: str, example: str) -> dict | None:
        text = re.sub(r"\s+", " ", str(wanted or "")).strip()
        name = canonical(text)
        if (not re.search(r"[a-z]", name) or len(name) > MAX_PROPOSAL_LEN
                or len(name.split("-")) > MAX_PROPOSAL_WORDS):
            return None
        known = self._match(name, self.everything)
        if known in self.active or known in self.rejected:
            return None
        return {"name": known or name, "label": text[:1].upper() + text[1:], "why": why.strip()[:200],
                "example": example.strip()[:120]}


def area_rules(catalog: AreaCatalog | None) -> str:
    catalog = catalog or AreaCatalog()
    rules = ("area: UNA sola, scelta tra queste (usa il nome esatto):\n" + catalog.prompt_block() +
             f"\nSe nessuna calza usa \"{FALLBACK_AREA}\". Solo in quel caso puoi proporre una NUOVA area ampia e "
             "ricorrente (non un singolo tema) con \"area_proposal\": {\"name\": \"nome breve\", \"why\": \"motivo in "
             "una frase\"}; altrimenti \"area_proposal\": null.")
    if catalog.rejected:
        rules += "\nAree già rifiutate dall'utente, non riproporle: " + ", ".join(sorted(catalog.rejected)) + "."
    return rules


def collect_proposals(state: State, entries: list[dict]) -> int:
    """Move the model's proposals out of the entries (they must not be stored with them) into the approval queue."""
    count = 0
    for entry in entries:
        proposal = entry.pop("area_proposal", None)
        if proposal:
            state.add_area_proposal(proposal["name"], proposal["label"], proposal["why"], proposal["example"])
            count += 1
    return count


@dataclass
class Entry:
    kind: str
    key: str
    text: str
    area: str


def _summary_of(analysis: dict) -> str:
    recipe = analysis.get("recipe") or {}
    parts = [analysis.get("summary") or "", " ".join(i.get("item", "") for i in recipe.get("ingredients", [])[:8])]
    return " ".join(p for p in parts if p).strip()[:240]


def reclassifiable_entries(state: State, target: str) -> list[Entry]:
    entries: list[Entry] = []
    for url, title, raw in state.db.execute("SELECT url, COALESCE(title,''), analysis FROM links WHERE status='done'"):
        analysis = json.loads(raw) if raw else {}
        if analysis.get("area_origin") == ORIGIN_MANUAL or analysis.get("area", FALLBACK_AREA) == target:
            continue
        tags = ", ".join(analysis.get("tags", [])[:8])
        entries.append(Entry("link", url, f"{title} — {_summary_of(analysis)} (tag: {tags})",
                             analysis.get("area", FALLBACK_AREA)))
    for item_id, title, details, tags_raw, area, origin in state.db.execute(
            "SELECT id, title, COALESCE(details,''), tags, COALESCE(area, ?), COALESCE(area_origin,'auto') FROM items",
            (FALLBACK_AREA,)):
        if origin == ORIGIN_MANUAL or area == target:
            continue
        tags = ", ".join(json.loads(tags_raw or "[]")[:8])
        entries.append(Entry("item", str(item_id), f"{title} — {details[:160]} (tag: {tags})", area))
    return entries


def reclassify(llm: LLM, cfg: Config, state: State, target: str, batch_size: int = RECLASSIFY_BATCH,
               progress=lambda message: None) -> int:
    """Look at everything already stored and move into `target` what clearly belongs there. Only moves INTO the new
    area (no churn elsewhere); areas chosen by hand are never touched; running it again is harmless."""
    area = state.get_area(target)
    if area is None or area["status"] != "active":
        raise ValueError(f"area {target!r} is not active")
    system = RECLASSIFY_SYSTEM.format(
        label=area["label"], description=area["description"] or "nessuna descrizione",
        others=", ".join(a["name"] for a in state.list_areas() if a["status"] == "active" and a["name"] != target))
    entries = reclassifiable_entries(state, target)
    moved = 0
    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        listing = "\n".join(f"[{n}] {e.text}" for n, e in enumerate(batch, 1))
        try:
            answer = llm.chat_json(system, listing)
        except LLMError as e:
            progress(f"batch {start // batch_size + 1}: {e}")
            continue
        numbers = {int(n) for n in answer.get("moves", []) if str(n).isdigit() and 1 <= int(n) <= len(batch)}
        for n in sorted(numbers):
            entry = batch[n - 1]
            (state.set_link_area if entry.kind == "link" else state.set_item_area)(
                entry.key if entry.kind == "link" else int(entry.key), target, ORIGIN_MODEL)
            moved += 1
        progress(f"{min(start + batch_size, len(entries))}/{len(entries)} voci riviste, {moved} spostate")
    return moved
