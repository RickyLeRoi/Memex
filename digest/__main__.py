from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .areas import AreaCatalog, collect_proposals, reclassify
from .config import Config, ConfigError, load_config
from .llm import LLM, LLMError
from .models import Doc, FetchResult
from .state import State
from .tags import Vocabulary
from .util import default_since, parse_iso

log = logging.getLogger("digest")
ALL_SOURCES = ("mail", "teams", "notion", "links", "slack", "slack_tickets", "gmail", "jira")
HERE = Path(__file__).resolve().parent


def setup_logging(cfg: Config | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg:
        handlers.append(logging.FileHandler(cfg.data_path / "digest.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True)
    for noisy in ("httpx", "httpcore", "msal", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)


def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists():
        print(f"{target} esiste già, non lo tocco.")
        return 1
    example = HERE / "config.example.toml"
    shutil.copy(example, target)
    links = target.parent / "links.txt"
    if not links.exists():
        links.write_text("# Un link per riga, eventuale nota dopo l'URL\n", encoding="utf-8")
    print(f"Creati {target} e {links}. Modifica la config e poi: python -m digest check")
    return 0


def cmd_login(cfg: Config, args) -> int:
    from .sources.msgraph import GraphAuth, GraphClient

    if not (cfg.mail.enabled or cfg.teams.enabled):
        print("Né [mail] né [teams] sono abilitati: niente login da fare.")
        return 0
    auth = GraphAuth(cfg)
    auth.token(interactive=True)
    me = GraphClient(auth.token).get("/me", {"$select": "displayName,mail,userPrincipalName"})
    print(f"Login ok: {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")
    return 0


def cmd_check(cfg: Config, args) -> int:
    llm = LLM(cfg.llm)
    print(f"Endpoint: {cfg.llm.base_url}")
    try:
        models = llm.list_models()
        print(f"Modelli disponibili: {', '.join(models) or '(nessuno)'}")
        if models and cfg.llm.model not in models:
            print(f"ATTENZIONE: '{cfg.llm.model}' non è nella lista.")
    except Exception as e:
        print(f"/models non disponibile ({e}), provo comunque la chat")
    try:
        out = llm.chat_json("Rispondi solo con JSON.", 'Restituisci {"ok": true, "lingua": "italiano"}')
        print(f"Chat JSON ok: {out}")
    except LLMError as e:
        print(f"ERRORE: {e}")
        return 1
    return 0


def cmd_add(cfg: Config, args) -> int:
    state = State(cfg.data_path / "state.sqlite")
    line = args.url + (f"  {args.note}" if args.note else "")
    with cfg.links_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    state.add_link(args.url, args.note or "")
    print(f"Aggiunto: {args.url}")
    return 0


def cmd_links(cfg: Config, args) -> int:
    state = State(cfg.data_path / "state.sqlite")
    if args.retry:
        state.db.execute("UPDATE links SET status='pending', attempts=0, error=NULL WHERE status='error'")
        state.db.commit()
        print("Link in errore rimessi in coda.")
    rows = state.db.execute(
        "SELECT status, attempts, url, COALESCE(title, ''), COALESCE(error, '') FROM links ORDER BY added_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for status, attempts, url, title, err in rows:
        print(f"[{status:7}] {url}  {title or err}")
    return 0


def _is_enabled(cfg: Config, src: str) -> bool:
    if src == "slack_tickets":
        return cfg.slack.enabled and bool(cfg.slack.ticket_channels)
    if src == "slack":
        return cfg.slack.enabled and bool(cfg.slack.channels)
    return getattr(cfg, src).enabled


def cmd_run(cfg: Config, args) -> int:
    from .extract import extract_items, highlights, summarize_link
    from .report import write_reports

    sources = [s.strip() for s in args.sources.split(",")] if args.sources else [
        s for s in ALL_SOURCES if _is_enabled(cfg, s)
    ]
    bad = set(sources) - set(ALL_SOURCES)
    if bad:
        print(f"Sorgenti sconosciute: {', '.join(bad)}")
        return 2

    state = State(cfg.data_path / "state.sqlite", ignore_seen=args.reprocess)
    llm = LLM(cfg.llm)
    vocabulary = Vocabulary(state.tag_counts(), curated=state.get_link_tags())
    catalog = AreaCatalog.from_state(state)
    started = datetime.now().astimezone()
    run_id = started.isoformat(timespec="seconds")
    failed_sources: list[str] = []
    result: dict = {"started": started, "stats": {}, "items": [], "links": [], "errors": [], "highlights": []}
    forced_since = parse_iso(args.since) if args.since else None
    if args.since and not forced_since:
        print("--since deve essere una data ISO, es. 2026-09-28 o 2026-09-28T09:00")
        return 2

    graph_holder: dict = {"g": None}  # one Graph client shared by mail and teams (single login)
    for src in sources:
        log.info("== %s ==", src)
        try:
            if src == "links":
                _run_links(cfg, state, llm, args, result, summarize_link, vocabulary, catalog)
                continue

            since = forced_since or parse_iso(state.get_cursor(src)) or default_since(cfg.initial_lookback_days)
            fr = _fetch(src, cfg, state, since, graph_holder)
            result["stats"][src] = len(fr.docs)
            log.info("%s: %d documenti nuovi dal %s", src, len(fr.docs), since.astimezone().strftime("%Y-%m-%d %H:%M"))

            if args.dry_run:
                _dump(cfg, src, fr.docs, started)
                continue
            items, ok_docs, errs = extract_items(llm, cfg, fr.docs, vocabulary, catalog) if fr.docs else ([], [], [])
            collect_proposals(state, items)  # 20261002 ++ RG #areas proposals wait for the user's approval
            result["items"] += items
            result["errors"] += [f"{src}: {e}" for e in errs]
            if not args.no_advance:
                _mark(state, src, ok_docs, fr.skipped_ids)
                if not errs and fr.cursor:
                    state.set_cursor(src, fr.cursor)
        except Exception as e:  # a broken source must not stop the others
            log.exception("Sorgente %s fallita", src)
            result["errors"].append(f"{src}: {e}")
            failed_sources.append(src)

    if args.dry_run:
        print(f"Dry run: testi salvati in {cfg.reports_path / 'dryrun'}")
        return 0

    if cfg.extract.final_summary and (result["items"] or result["links"]):
        try:
            result["highlights"] = highlights(llm, cfg, result["items"], result["links"])
        except LLMError as e:
            result["errors"].append(f"riepilogo: {e}")

    state.save_items(run_id, result["items"])
    path = write_reports(result, cfg.reports_path)
    print(f"Report: {path}")
    if cfg.obsidian.enabled:
        from .obsidian import write_obsidian

        try:
            excluded = {a["name"] for a in state.list_areas() if a["exclude_from_vault"]}
            note = write_obsidian(result, cfg, excluded)
            print(f"Obsidian: {note}")
        except OSError as e:
            log.error("Scrittura nel vault fallita: %s", e)
            return 1
    return 1 if failed_sources else 0


def cmd_reclassify(cfg: Config, args) -> int:
    state = State(cfg.data_path / "state.sqlite")
    try:
        moved = reclassify(LLM(cfg.llm), cfg, state, args.area, progress=print)
    except ValueError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 2
    finally:
        state.close()
    print(f"Spostate {moved} voci nell'area {args.area}.")
    return 0


def cmd_serve(cfg: Config, args) -> int:
    from .web.server import create_server
    from .web.service import Service

    # 20261002 ++ RG #docker the GUI reads mail and chats and can delete data: never open to the network without a token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not cfg.server.token \
            and os.environ.get("DIGEST_ALLOW_UNAUTHENTICATED") != "1":
        print(f"Rifiuto di ascoltare su {args.host} senza un token: imposta DIGEST_GUI_TOKEN (o [server] token). "
              "Se è dietro una porta pubblicata solo su loopback, DIGEST_ALLOW_UNAUTHENTICATED=1 lo consente.",
              file=sys.stderr)
        return 2
    if cfg.server.token and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Nota: il token viaggia in chiaro su HTTP. Fuori dalla tua LAN mettilo dietro un reverse proxy TLS.")
    server = create_server(Service(cfg, Path(args.config).resolve()), args.host, args.port)
    shown = f"[{args.host}]" if ":" in args.host else args.host  # 20261002 ** RG #docker IPv6 literal
    print(f"GUI su http://{shown}:{args.port}  (Ctrl+C per fermare)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFermato.")
    finally:
        server.server_close()
    return 0


def _fetch(src: str, cfg: Config, state: State, since, holder: dict) -> FetchResult:
    if src in ("mail", "teams"):
        from .sources.msgraph import GraphAuth, GraphClient, fetch_mail, fetch_teams

        if holder["g"] is None:
            holder["g"] = GraphClient(GraphAuth(cfg).token)
        return (fetch_mail if src == "mail" else fetch_teams)(holder["g"], cfg, state, since)
    if src == "notion":
        from .sources.notion import NotionClient, fetch_notion

        return fetch_notion(NotionClient(cfg.notion.token), cfg, state, since)
    if src in ("slack", "slack_tickets"):
        import httpx

        from .sources.slack import SlackClient, fetch_slack

        return fetch_slack(SlackClient(cfg.slack.token, httpx.Client(timeout=60)), cfg, state, since, src)
    if src == "gmail":
        from .sources.gmail import fetch_gmail

        return fetch_gmail(cfg, state, since)
    if src == "jira":
        from .sources.jira import JiraClient, fetch_jira

        return fetch_jira(JiraClient(cfg.jira.base_url, cfg.jira.email, cfg.jira.api_token), cfg, state, since)
    raise ValueError(src)


def _mark(state: State, src: str, docs: list[Doc], skipped: list[str]) -> None:
    ids: list[tuple[str, str | None]] = [(i, None) for i in skipped]
    for d in docs:
        if src in ("teams", "slack", "slack_tickets"):
            ids += [(m, None) for m in d.meta.get("msg_ids", [])]
        else:
            ids.append((d.id, d.meta.get("fp")))
    if ids:
        state.mark_seen(src, ids)


def _run_links(cfg: Config, state: State, llm: LLM, args, result: dict, summarize_link,
               vocabulary: Vocabulary | None = None, catalog: AreaCatalog | None = None) -> None:
    from .sources.links import fetch_links

    docs, errors = fetch_links(cfg, state, llm if not args.dry_run else None,
                               store_images=not args.dry_run)
    result["stats"]["link"] = len(docs)
    result["errors"] += [f"link {e}" for e in errors]
    if args.dry_run:
        _dump(cfg, "links", docs, result["started"])
        return
    for d in docs:
        try:
            ln = summarize_link(llm, cfg, d, vocabulary, catalog)
        except LLMError as e:
            state.link_failed(d.url, str(e))
            result["errors"].append(f"link {d.url}: {e}")
            continue
        collect_proposals(state, [ln])
        ln["image"] = d.meta.get("image")
        result["links"].append(ln)
        if not args.no_advance:
            state.link_done(d.url, ln["title"], ln)
        # 20261002 ++ RG #documents deadlines and tasks; a recipe has none, and must not be restated as an "info" item
        if d.meta.get("platform") in ("screenshot", "documento") and ln.get("kind") != "recipe":
            from .extract import extract_items

            items, _, item_errors = extract_items(llm, cfg, [d], vocabulary, catalog)
            collect_proposals(state, items)
            result["items"] += items
            result["errors"] += [f"{d.url}: {e}" for e in item_errors]


def _dump(cfg: Config, src: str, docs: list[Doc], started: datetime) -> None:
    d = cfg.reports_path / "dryrun"
    d.mkdir(exist_ok=True)
    path = d / f"{started.strftime('%Y-%m-%d_%H%M')}_{src}.txt"
    path.write_text("\n\n==========\n\n".join(f"[{x.source}] {x.title}\n{x.url}\n\n{x.text}" for x in docs),
                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="digest", description="Digest locale di Teams, Outlook, Notion e link salvati.")
    p.add_argument("-c", "--config", default="config.toml", help="percorso config (default: ./config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="crea config.toml e links.txt di esempio")
    sub.add_parser("login", help="login Microsoft (device code), da fare una volta")
    sub.add_parser("check", help="verifica che l'endpoint LLM locale risponda")
    a = sub.add_parser("add", help="aggiunge un link da analizzare")
    a.add_argument("url")
    a.add_argument("note", nargs="?", default="", help="nota opzionale: perché l'hai salvato")
    lk = sub.add_parser("links", help="stato della coda link")
    lk.add_argument("--retry", action="store_true", help="rimette in coda i link falliti")
    lk.add_argument("--limit", type=int, default=30)
    rc = sub.add_parser("reclassify", help="rivede tutto l'archivio per una nuova area approvata")
    rc.add_argument("--area", required=True)
    sv = sub.add_parser("serve", help="avvia l'interfaccia grafica locale")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    r = sub.add_parser("run", help="esegue il digest")
    r.add_argument("--sources", help=f"sottoinsieme di {','.join(ALL_SOURCES)}")
    r.add_argument("--since", help="ignora i cursori e parte da questa data ISO")
    r.add_argument("--reprocess", action="store_true",
                   help="rielabora anche elementi già visti (da usare con --since)")
    r.add_argument("--dry-run", action="store_true", help="scarica e salva i testi senza chiamare il modello")
    r.add_argument("--no-advance", action="store_true", help="non aggiorna cursori/visti (utile per provare prompt)")

    args = p.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(args)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Errore di configurazione: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg, args.verbose)
    handlers = {"login": cmd_login, "check": cmd_check, "add": cmd_add, "links": cmd_links, "run": cmd_run,
                "serve": cmd_serve, "reclassify": cmd_reclassify}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
