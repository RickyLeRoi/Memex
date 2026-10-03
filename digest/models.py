from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Doc:

    source: str
    id: str
    title: str
    text: str
    url: str = ""
    timestamp: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    docs: list[Doc]
    cursor: str | None = None  # new cursor, saved only if the analysis succeeds
    skipped_ids: list[str] = field(default_factory=list)
