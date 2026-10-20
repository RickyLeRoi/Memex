"""Tags as a shared vocabulary between the user and the local LLM.

- `canonical` normalizes a tag (case, accents, separators).
- `Vocabulary.resolve` maps a variant to an existing tag (singular/plural, case, accents) without calling the LLM,
  so the vocabulary does not fragment when the model ignores the hint.
- `Vocabulary.prompt_block` is what the model is shown: curated linking tags first, then most used.
- `Vocabulary.finalize` guarantees every piece of information ends up with at least one tag.
"""

from __future__ import annotations

import re
import unicodedata

MAX_TAG_LEN = 40
MAX_TAGS_PER_ENTRY = 6
PROMPT_MAX_TAGS = 150
PROMPT_MAX_CHARS = 1500
MIN_LEN_FOR_SUFFIX_SWAP = 5  # "casa"/"caso" must not be merged by accident
MIN_LEN_FOR_PLURAL_S = 4
GENERIC_TAGS = {"ingredienti", "ingredient", "ingredients", "tag", "tags", "varie", "altro", "other", "misc"}
ORIGIN_MODEL, ORIGIN_AUTO, ORIGIN_MANUAL = "model", "auto", "manual"

_SUFFIX_SWAPS = {"a": ("e",), "e": ("a", "i"), "o": ("i",), "i": ("o", "e")}
_VOWELS = "aeiou"
# Italian spelling: the hard c/g keeps its sound in the plural (fresca/fresche, tecnico/tecnici, lungo/lunghi)
_ORTHOGRAPHY = (("ca", "che"), ("ga", "ghe"), ("co", "chi"), ("go", "ghi"), ("co", "ci"), ("go", "gi"),
                ("cia", "ce"), ("gia", "ge"))


def canonical(tag: object) -> str:
    text = unicodedata.normalize("NFKD", str(tag or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:MAX_TAG_LEN].strip("-")


def _variants(tag: str) -> set[str]:
    head, _, last = tag.rpartition("-")
    found: set[str] = set()
    if len(last) >= MIN_LEN_FOR_SUFFIX_SWAP:
        found |= {last[:-1] + swap for swap in _SUFFIX_SWAPS.get(last[-1], ())}
        for singular, plural in _ORTHOGRAPHY:
            if last.endswith(singular):
                found.add(last[: -len(singular)] + plural)
            if last.endswith(plural):
                found.add(last[: -len(plural)] + singular)
    if len(last) >= MIN_LEN_FOR_PLURAL_S:
        if last.endswith("s"):
            found.add(last[:-1])
        elif last[-1] not in _VOWELS:
            found.add(last + "s")
    prefix = f"{head}-" if head else ""
    return {prefix + v for v in found}


def as_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    return [str(v) for v in value if v is not None] if isinstance(value, (list, tuple)) else []


class Vocabulary:
    def __init__(self, counts: dict[str, int] | None = None, curated: list[str] | set[str] | tuple = ()):
        self.counts: dict[str, int] = {}
        for tag, n in (counts or {}).items():
            if canonical(tag):
                self.counts[canonical(tag)] = self.counts.get(canonical(tag), 0) + n
        self.curated = {canonical(t) for t in curated if canonical(t)}
        for tag in self.curated:
            self.counts.setdefault(tag, 0)

    def resolve(self, tag: object) -> str:
        wanted = canonical(tag)
        if not wanted or wanted in self.counts:
            return wanted
        existing = [v for v in _variants(wanted) if v in self.counts]
        if not existing:
            return wanted
        return max(existing, key=lambda v: (v in self.curated, self.counts[v], v))

    def add(self, tags: list[str]) -> None:
        for tag in tags:
            self.counts[tag] = self.counts.get(tag, 0) + 1

    def ordered(self) -> list[str]:
        return sorted(self.counts, key=lambda t: (t not in self.curated, -self.counts[t], t))

    def prompt_block(self) -> str:
        chosen: list[str] = []
        size = 0
        for tag in self.ordered()[:PROMPT_MAX_TAGS]:
            if size + len(tag) + 2 > PROMPT_MAX_CHARS:
                break
            chosen.append(tag)
            size += len(tag) + 2
        return ", ".join(chosen)

    def finalize(self, raw: object, *, source: str, kind: str = "", fallback_extra: tuple[str, ...] = (),
                 limit: int = MAX_TAGS_PER_ENTRY) -> tuple[list[str], dict[str, str]]:
        """Return (tags, origin per tag). Never empty: with no usable model tag, auto tags are used."""
        tags: list[str] = []
        for candidate in as_list(raw):
            resolved = self.resolve(candidate)
            if resolved and resolved not in GENERIC_TAGS and resolved not in tags:
                tags.append(resolved)
        tags = tags[:limit]
        if tags:
            self.add(tags)  # model tags join the vocabulary; auto tags never do (they would pollute the prompt)
            return tags, {t: ORIGIN_MODEL for t in tags}
        auto = list(dict.fromkeys(c for c in (canonical(x) for x in (source, kind, *fallback_extra)) if c))
        return auto, {t: ORIGIN_AUTO for t in auto}


def tag_rules(vocabulary: Vocabulary | None) -> str:
    rules = ("tag: da 2 a 5, minuscolo, parole unite da trattino. Un tag nomina un argomento, strumento, progetto o "
             "tecnologia citato nel testo, non solo il tema principale. Mai tag generici come 'varie' o 'altro'.")
    block = vocabulary.prompt_block() if vocabulary else ""
    if block:
        rules += ("\nTag già in uso (riusa quello esistente se è semanticamente adatto; creane uno nuovo solo se "
                  f"nessuno calza): {block}")
    return rules
