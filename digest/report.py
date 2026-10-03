from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PRIO_ORDER = {"alta": 0, "media": 1, "bassa": 2}
SOURCE_LABEL = {"mail": "mail", "teams": "Teams", "notion": "Notion", "link": "link", "?": "?",
                "slack": "Slack", "slack_tickets": "Slack (ticket)", "gmail": "Gmail", "jira": "Jira"}


INTERNAL_SCHEMES = ("image://", "file://")  # 20261002 ++ RG #screenshots #documents references to local uploads


def is_internal_ref(url: str) -> bool:
    return url.startswith(INTERNAL_SCHEMES)


def sort_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (PRIO_ORDER.get(i["priority"], 1), i.get("due") or "9999", i["title"]))


def item_line(it: dict, checkbox: bool = False) -> str:
    prefix = "- [ ] " if checkbox else "- "
    prio = "🔴 " if it["priority"] == "alta" and not checkbox else ""
    line = f"{prefix}{prio}**{it['title']}**"
    if it.get("details"):
        line += f" — {it['details']}"
    extra = []
    if it.get("owner") and not it.get("mine"):
        extra.append(f"chi: {it['owner']}")
    if it.get("due"):
        extra.append(f"entro {it['due']}")
    src = SOURCE_LABEL.get(it.get("source", "?"), it.get("source", "?"))
    ref = it.get("ref_title") or ""
    ref = ref if len(ref) <= 60 else ref[:57] + "…"
    linkable = it.get("ref_url") and not is_internal_ref(it["ref_url"])
    extra.append(f"[{src}: {ref}]({it['ref_url']})" if linkable else f"{src}: {ref}")
    line += f" _({' · '.join(extra)})_"
    if it.get("tags"):
        line += " " + " ".join(f"#{t}" for t in it["tags"])
    return line


def sections(items: list[dict]) -> list[tuple[str, list[dict]]]:
    items = sort_items(items)
    mine = [i for i in items if i.get("mine") and i["kind"] in ("task", "question", "deadline")]
    mine_ids = {id(i) for i in mine}
    rest = [i for i in items if id(i) not in mine_ids]
    by = lambda *k: [i for i in rest if i["kind"] in k]
    return [
        ("Tocca a te", mine),
        ("Scadenze", by("deadline")),
        ("Task di altri / da seguire", by("task")),
        ("Domande aperte", by("question")),
        ("Decisioni", by("decision")),
        ("Info utili", by("info")),
        ("Idee", by("idea")),
    ]


def recipe_lines(recipe: dict) -> list[str]:
    servings = recipe.get("servings") or ""
    facts = [f for f in (recipe.get("cuisine"), f"{servings} porzioni" if servings.isdigit() else servings,
                         recipe.get("time")) if f]
    out = [f"_{' · '.join(facts)}_", ""] if facts else []
    if recipe.get("ingredients"):
        out += ["**Ingredienti**", ""] + [f"- {i['text']}" for i in recipe["ingredients"]] + [""]
    if recipe.get("steps"):
        out += ["**Preparazione**", ""] + [f"{n}. {s}" for n, s in enumerate(recipe["steps"], 1)] + [""]
    if recipe.get("tips"):
        out += ["**Consigli**", ""] + [f"- {t}" for t in recipe["tips"]] + [""]
    return out


def link_block(ln: dict, heading: str = "###") -> str:
    # 20261002 ** RG #screenshots #documents uploaded screenshots/PDFs have no web address to link to
    out = [f"{heading} {ln['title']}" if is_internal_ref(ln["url"]) else f"{heading} [{ln['title']}]({ln['url']})"]
    meta = [ln.get("platform", "")]
    if ln.get("tags"):
        meta.append(" ".join(f"#{t}" for t in ln["tags"]))
    out.append(f"_{' · '.join(m for m in meta if m)}_")
    if ln.get("note"):
        out.append(f"> Tua nota: {ln['note']}")
    out.append("")
    if ln.get("kind") == "recipe" and ln.get("recipe"):  # 20261002 ++ RG #recipes a recipe is listed, never summarized
        return "\n".join(out + recipe_lines(ln["recipe"])).rstrip()
    out.append(ln.get("summary", ""))
    if ln.get("key_points"):
        out.append("")
        out += [f"- {p}" for p in ln["key_points"]]
    if ln.get("actions"):
        out.append("")
        out.append("**Da provare:**")
        out += [f"- {a}" for a in ln["actions"]]
    return "\n".join(out)


def render_markdown(result: dict) -> str:
    started: datetime = result["started"]
    md = [f"# Digest del {started.strftime('%Y-%m-%d %H:%M')}", ""]
    stats = result["stats"]
    if stats:
        md.append("_" + " · ".join(f"{k}: {v}" for k, v in stats.items()) + "_")
        md.append("")
    if result.get("highlights"):
        md += ["## In evidenza", ""] + [f"- {h}" for h in result["highlights"]] + [""]
    for title, its in sections(result["items"]):
        if its:
            md += [f"## {title}", ""] + [item_line(i) for i in its] + [""]
    if result["links"]:
        md += ["## Link salvati", ""]
        for ln in result["links"]:
            md += [link_block(ln), ""]
    if result["errors"]:
        md += ["## Problemi", ""] + [f"- {e}" for e in result["errors"]] + [""]
    if not (result["items"] or result["links"] or result["errors"]):
        md += ["Niente di nuovo. Goditi il silenzio.", ""]
    return "\n".join(md)


def write_reports(result: dict, out_dir: Path) -> Path:
    stamp = result["started"].strftime("%Y-%m-%d_%H%M")
    md_path = out_dir / f"digest_{stamp}.md"
    md = render_markdown(result)
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "latest.md").write_text(md, encoding="utf-8")
    payload = {k: v for k, v in result.items() if k != "started"}
    payload["started"] = result["started"].isoformat()
    (out_dir / f"digest_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md_path
