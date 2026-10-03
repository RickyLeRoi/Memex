"""Recipes: a structured extraction, never a summary.

A recipe is kept as ingredients (with their quantities), numbered steps and the author's tips. When the page carries a
schema.org Recipe (JSON-LD) that data is exact and wins over what the model returns; the model fills what is missing
(tips, cuisine) and reads recipes that have no JSON-LD (captions, screenshots). Small models invent numbers, so a
quantity that does not appear in the source text is dropped instead of being trusted.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date

from bs4 import BeautifulSoup

from .config import Config
from .llm import LLM
from .models import Doc
from .tags import Vocabulary, canonical
from .util import truncate

RECIPE_TAG_LIMIT = 20
MAX_NAME_WORDS = 3

RECIPE_SYSTEM = """Estrai una ricetta dal contenuto. Oggi è {today}.

Rispondi SOLO con un oggetto JSON:
{{"title": "titolo della ricetta", "cuisine": "tipo di cucina in una o due parole (italiana, giapponese, messicana, vegana...) oppure null",
  "servings": "porzioni se indicate, altrimenti null", "time": "tempo di preparazione/cottura se indicato, altrimenti null",
  "ingredients": [{{"quantity": "quantità ESATTAMENTE come scritta (es. '200 g', '2 cucchiai', 'q.b.') oppure ''", "item": "solo il nome dell'ingrediente"}}],
  "steps": ["primo passaggio", "secondo passaggio"],
  "tips": ["consigli, varianti o note dell'autore"]}}

Regole:
- NON riassumere. Elenca TUTTI gli ingredienti e TUTTI i passaggi, nell'ordine del testo e con le sue parole.
- Copia le quantità esattamente: non convertire, non stimare, non inventare. Se manca, scrivi "".
- tips solo se il testo contiene davvero consigli, varianti o note: altrimenti lista vuota. Non aggiungere "cerca altre ricette" o simili.
- Se un dato non c'è nel testo non inventarlo. Scrivi in {language}."""

_STOPWORDS = {"di", "d", "del", "della", "dello", "dei", "delle", "da", "al", "alla", "e", "a", "in", "con", "the", "of"}
_ADJECTIVES = {
    "fresco", "fresca", "freschi", "fresche", "maturo", "matura", "maturi", "mature", "grande", "grandi", "piccolo",
    "piccola", "piccoli", "piccole", "medio", "media", "medi", "grosso", "grossa", "tritato", "tritata", "tritati",
    "tritate", "grattugiato", "grattugiata", "fresh", "ripe", "large", "small", "medium", "chopped", "minced", "sliced",
    "grated", "whole", "intero", "intera", "interi",
}
_UNITS = (
    r"kg|g|gr|grammi|mg|ml|l|cl|dl|litri?|litro|cups?|tbsps?|tsps?|oz|lbs?|cucchiai[oa]?|cucchiaini?|cucchiaio|spicchi[o]?|"
    r"pizzic[oh]i?|bicchieri?|tazz[ae]|fett[ae]|foglie|foglia|rametti|rametto|mazzett[oi]|pugn[oi]|barattol[oi]|"
    r"lattin[ae]|bustin[ae]|confezion[ei]|cubett[oi]|noci|noce"
)
_QUANTITY_PREFIX = re.compile(
    rf"^\s*(?:un[oa]?\s+)?(?:q\.?\s?b\.?|a piacere|[\d½¼¾⅓⅔][\d/.,\s½¼¾⅓⅔-]*)?\s*(?:(?:{_UNITS})\b\.?)?\s*(?:di\s+|d'|of\s+)?",
    re.IGNORECASE,
)


def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(text or "")))).strip()


def _iso_duration(value: object) -> str:
    match = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(value or "").strip())
    if not match or not any(match.groups()):
        return ""
    hours, minutes = match.groups()
    return " ".join(part for part in (f"{hours} h" if hours else "", f"{minutes} min" if minutes else "") if part)


def _instruction_steps(value: object) -> list[str]:
    steps: list[str] = []
    if isinstance(value, str):
        pieces = [p for p in re.split(r"\r?\n+", value) if p.strip()] if "\n" in value else [value]
        steps += [_clean(p) for p in pieces]
    elif isinstance(value, list):
        for entry in value:
            steps += _instruction_steps(entry)
    elif isinstance(value, dict):
        if value.get("itemListElement"):
            steps += _instruction_steps(value["itemListElement"])
        else:
            steps.append(_clean(value.get("text") or value.get("name")))
    cleaned = [re.sub(r"^\s*(?:\d+\s*[.)]|step\s*\d+\s*[:.]?)\s*", "", s, flags=re.IGNORECASE) for s in steps]
    return [s for s in cleaned if s]


def _as_text_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [_clean(value)] if value.strip() else []
    return [_clean(v) for v in value if _clean(v)] if isinstance(value, list) else []


def _is_recipe_node(node: dict) -> bool:
    kind = node.get("@type")
    return kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind)


def _find_recipe(node: object) -> dict | None:
    if isinstance(node, dict):
        if _is_recipe_node(node):
            return node
        for child in node.values():
            found = _find_recipe(child) if isinstance(child, (dict, list)) else None
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_recipe(child)
            if found:
                return found
    return None


def recipe_from_jsonld(page_html: str) -> dict | None:
    for script in BeautifulSoup(page_html, "lxml").find_all("script", type="application/ld+json"):
        try:
            node = _find_recipe(json.loads(script.string or script.get_text() or ""))
        except json.JSONDecodeError:
            continue
        if not node:
            continue
        cuisine = _as_text_list(node.get("recipeCuisine"))
        yield_ = _as_text_list(node.get("recipeYield"))
        times = [t for t in (_iso_duration(node.get("totalTime")),) if t] or [
            _iso_duration(node.get("prepTime")), _iso_duration(node.get("cookTime"))]
        recipe = {
            "title": _clean(node.get("name")),
            "cuisine": cuisine[0] if cuisine else "",
            "servings": yield_[0] if yield_ else "",
            "time": " + ".join(t for t in times if t),
            "ingredients": _as_text_list(node.get("recipeIngredient") or node.get("ingredients")),
            "steps": _instruction_steps(node.get("recipeInstructions")),
        }
        if recipe["ingredients"] or recipe["steps"]:
            return recipe
    return None


def ingredient_name(line: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", line.lower()).split(",")[0]
    return _shorten(_QUANTITY_PREFIX.sub("", text, count=1))


def _shorten(name: str) -> str:
    words = [w for w in re.split(r"[\s']+", name.strip(" -–:.")) if w and w not in _STOPWORDS and w not in _ADJECTIVES]
    return " ".join(words[:MAX_NAME_WORDS])


def quantity_in_source(quantity: str, source: str) -> bool:
    """A quantity is trusted only if its numbers (or its words) really appear in the source text."""
    quantity = quantity.strip()
    if not quantity:
        return True
    haystack = source.lower().replace(",", ".")
    numbers = re.findall(r"\d+(?:[./]\d+)?", quantity.lower().replace(",", "."))
    if numbers:  # whole numbers only: "2" must not be found inside "200" or "1.25"
        return all(re.search(rf"(?<![\d.]){re.escape(n)}(?!\d)", haystack) for n in numbers)
    return quantity.lower() in source.lower()


def _optional(value: object) -> str:
    """Optional field as text; small models write the string 'null' instead of a JSON null."""
    text = _clean(value)
    return "" if text.lower() in ("null", "none", "n/a", "nan", "-", "non indicato", "non specificato") else text


def _verbatim_span(quantity: str, item: str, source: str) -> str:
    pattern = re.escape(quantity) + r"\s*(?:di\s+|d')?\s*" + re.escape(item) if quantity else None
    match = re.search(pattern, source, flags=re.IGNORECASE) if pattern else None
    return _clean(match.group(0)) if match else f"{quantity} {item}".strip()


def _llm_ingredients(raw: object, source: str) -> list[dict]:
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            entry = {"quantity": "", "item": entry}
        if not isinstance(entry, dict) or not _clean(entry.get("item")):
            continue
        quantity = _optional(entry.get("quantity"))
        if not quantity_in_source(quantity, source):
            quantity = ""  # invented by the model
        item = _clean(entry.get("item"))
        out.append({"text": _verbatim_span(quantity, item, source), "item": item.lower()})
    return out


_SECTION_END = r"(?:preparazione|procedimento|istruzioni|metodo|come si prepara|directions|instructions|method|steps|consigli)"
MAX_LIST_LINE = 80
MAX_LIST_LINES = 40


def ingredients_from_text(text: str) -> list[str]:
    """Ingredient lines read straight from an 'Ingredienti: ...' section, verbatim. Empty if there is no clean list.

    Small models skip items from long lists, so a plainly formatted list is read without them; the model's reading is
    used only when the text has no such section."""
    clean = re.sub(r"[*_#`>]+", "", text)
    # the label with a colon anywhere in the text, or the label alone on its own line
    start = (re.search(r"(?i)\b(?:ingredienti|ingredients)\b[^\n:]{0,40}:[ \t]*", clean)
             or re.search(r"(?im)^[ \t\-•\d.)]*(?:ingredienti|ingredients)\b[^\n]{0,40}$\n?", clean))
    if not start:
        return []
    rest = clean[start.end():]
    section_end = re.search(rf"(?im)^[ \t\-•\d.)]*{_SECTION_END}\b|\b{_SECTION_END}\s*:", rest)
    block = rest[:section_end.start()] if section_end else rest[:600]
    if "\n" not in block.strip():  # one-line list: it ends where the sentence ends ("q.b." is not a sentence end)
        sentence_end = re.search(r"\.\s+(?=[A-ZÀ-Ý])", block)
        if sentence_end:  # keep the dot that belongs to "q.b."
            block = block[:sentence_end.start() + (1 if block[:sentence_end.start()].endswith("q.b") else 0)]
    block = block.strip()
    pieces = re.split(r"\r?\n", block) if "\n" in block else re.split(r"\s*[,;]\s*", block)
    # only real list markers go ("- ", "• ", "1. ", "2) "): a leading quantity such as "200 g" must survive
    lines = [re.sub(r"(?<!q\.b)\.$", "", re.sub(r"^\s*(?:[-•*]\s*|\d+\s*[.)]\s+)", "", p).strip(" ;")).strip()
             for p in pieces]
    lines = [line for line in lines if line]
    if not lines or len(lines) > MAX_LIST_LINES or any(len(line) > MAX_LIST_LINE for line in lines):
        return []  # not a clean list: do not guess
    return lines


def _merge_ingredients(lines: list[str], from_model: list[dict]) -> list[dict]:
    """JSON-LD lines are kept verbatim; the model's item name is used when it really is in the line."""
    merged = []
    for line in lines:
        model_item = next((m["item"] for m in from_model if m["item"] and m["item"] in line.lower()), "")
        merged.append({"text": line, "item": model_item or ingredient_name(line)})
    return merged


def extract_recipe(llm: LLM, cfg: Config, doc: Doc, jsonld: dict | None = None) -> dict:
    """Structured recipe: JSON-LD wins where present, the model fills the rest. Raises LLMError like any model call."""
    system = RECIPE_SYSTEM.format(today=date.today().isoformat(), language=cfg.extract.language)
    out = llm.chat_json(system, truncate(doc.text, cfg.links.max_chars))
    model_ingredients = _llm_ingredients(out.get("ingredients"), doc.text)
    model_steps = _instruction_steps(out.get("steps"))
    recipe = {
        "title": _clean(out.get("title")), "cuisine": _optional(out.get("cuisine")),
        "servings": _optional(out.get("servings")), "time": _optional(out.get("time")),
        "ingredients": model_ingredients, "steps": model_steps, "tips": _as_text_list(out.get("tips")),
    }
    listed = ingredients_from_text(doc.text)
    if listed and len(listed) >= len(model_ingredients):  # a plain list in the text is more complete than the model
        recipe["ingredients"] = _merge_ingredients(listed, model_ingredients)
    if jsonld:
        for key in ("title", "cuisine", "servings", "time"):
            recipe[key] = jsonld.get(key) or recipe[key]
        if jsonld["ingredients"]:
            recipe["ingredients"] = _merge_ingredients(jsonld["ingredients"], model_ingredients)
        if jsonld["steps"]:
            recipe["steps"] = jsonld["steps"]
    return recipe


def recipe_tags(recipe: dict, vocabulary: Vocabulary) -> tuple[list[str], dict[str, str]]:
    raw: list[str] = []
    cuisine = canonical(re.sub(r"^\s*cucina\s+", "", recipe.get("cuisine", ""), flags=re.IGNORECASE))
    if cuisine:
        raw.append(f"cucina-{cuisine}")
    raw += [_shorten(ingredient["item"].lower()) for ingredient in recipe.get("ingredients", []) if ingredient.get("item")]
    return vocabulary.finalize(raw, source="link", fallback_extra=("ricetta",), limit=RECIPE_TAG_LIMIT)
