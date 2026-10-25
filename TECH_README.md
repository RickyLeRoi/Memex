# Memex - technical notes

How Memex is built, how data moves through it, the two-endpoint LLM model for
links, and how it is deployed. For installation and day-to-day use see
[README.md](README.md) (in Italian).

## Overview

Memex reads sources (mail, chats, tickets, pages, saved links, uploaded images
and PDFs), asks an LLM to extract what matters, and writes the result to a
SQLite database, Markdown/JSON reports and optionally an Obsidian vault. A small
local web server exposes a React GUI and a JSON API over the same data.

```
sources/*  ->  Doc objects  ->  LLM extraction  ->  state.sqlite
                                                       |-> reports/*.md|json
                                                       |-> Obsidian vault
                                                       \-> web API + GUI
```

Python 3.11+ (3.12 in the image). Core dependencies: `httpx`, `msal`,
`beautifulsoup4`, `lxml`, `trafilatura`; optional extras for `yt-dlp`
(social), Playwright/Chromium (render) and PDF reading. The server is `http.server`, not a
framework. The frontend is React + Vite + TypeScript with no chart library.

## Modules

| Path | Role |
|---|---|
| `digest/__main__.py` | CLI: `init`, `login`, `check`, `add`, `links`, `run`, `serve`, `reclassify` |
| `digest/config.py` | TOML sections as dataclasses; environment overrides on top |
| `digest/llm.py` | `LLM`, `FallbackLLM`, `LinkLLM`; JSON parsing, vision, model resolution |
| `digest/extract.py` | Prompts, chunking, normalisation, link summaries |
| `digest/recipes.py` | Structured recipes (schema.org JSON-LD first, model for the rest) |
| `digest/state.py` | SQLite: cursors, seen ids, link queue and history, areas, tags |
| `digest/areas.py`, `tags.py` | Controlled vocabularies and reclassification |
| `digest/sources/*` | One module per source; `links.py` is the web, Instagram, Threads, video fetcher |
| `digest/web/server.py` | HTTP handler, authentication, routes |
| `digest/web/service.py` | The operations behind the routes; runs ingest jobs |
| `frontend/` | The GUI |
| `scripts/deploy.sh` | Local build and restart on the homelab VM |

## Link pipeline

1. A link enters the queue: GUI, `links.txt`, `digest add`, or
   `POST /api/ingest/links`. `State.add_link` inserts a row in `links` with the
   privacy flag; an existing URL keeps its first flag.
2. A run (`digest run`, started by the GUI as a subprocess or by a scheduler)
   syncs `links.txt` into the table and calls `fetch_links`.
3. For each pending link `LinkFetcher` tries strategies in order: `yt-dlp` for
   video, an Instagram carousel reader in Chromium (with cookies from
   `cookies.txt`), a direct download, then a Chromium render. Images found on a
   page are described by the vision model.
4. The result becomes a `Doc`; `summarize_link` asks the LLM for title, summary,
   key points, tags, area, or a structured recipe.
5. `State.link_done` stores the analysis; reports and the vault are written.

A link that fails stays in the queue until `max_attempts`.

## LLM layer and privacy

Two endpoints can be configured:

- `[llm]`: the local one (Ollama). Always present. Used for every source other
  than links, and for private links.
- `[llm_shared]`: optional, normally the `onfeather-free` router. Used only for
  non-private links.

`LinkLLM` holds both. `for_link(private)` returns the local `LLM` for a private
link (or when no shared endpoint is configured), and a `FallbackLLM(shared,
local)` otherwise. `FallbackLLM` forwards `chat`, `chat_json` and
`describe_image` to the shared endpoint and, on any `LLMError` or HTTP error,
repeats the same call on the local one. The fallback is per call, so a carousel
of sixteen slides can be read partly by each endpoint.

`fetch_links` picks the LLM per URL through `llm_for_link(llm, state, url)`; the
summary step does the same. A plain `LLM` (older tests, `--dry-run`) is served
unchanged because the function checks for `for_link`.

The privacy flag is the `links.private` column (added by an automatic
`ALTER TABLE` for existing databases). It is set from the API (`"private": true`
in the link entry, `X-Private: 1` on uploads) or the GUI checkboxes. Default is
shared. Content sent to a free provider can be used for training by that
provider; private links never leave the machine.

### Model names

A configured model name of the form `first:<provider>` (for example
`first:google`) is resolved at run time: `LLM._model` lists `/models` once and
takes the first `<provider>/...` entry. A 400, 404 or 410 on that model marks it
gone and the next call resolves the next candidate; with none left the call
raises `LLMError`. The router does not publish capabilities, so "first Google
model" is a convention.

### Environment overrides

`config.load_config` applies variables over the file. Empty values are ignored,
because docker compose passes unset variables through as empty strings and an
empty API key used to produce an `Authorization: Bearer ` header and "Illegal
header value" errors. The shared endpoint reads `DIGEST_LLM_SHARED_BASE_URL`,
`_MODEL`, `_API_KEY`, `_VISION_MODEL`.

## Web server

`create_server` builds a `ThreadingHTTPServer`. Every request goes through
`_check_host` (the `Host` header must be loopback or in `allowed_hosts` /
`DIGEST_ALLOWED_HOSTS`, else 403) and `_check_auth` (HTTP Basic, any user name,
the password compared in constant time with `DIGEST_GUI_TOKEN`). `/healthz` is
open. Binding to a non-loopback address without a token is refused at startup.

Routes that matter for integrations:

| Route | Purpose |
|---|---|
| `POST /api/ingest/links` | `{"links": [{url, note, private}], "queue_only"?: true}`; without `queue_only` it also starts an ingest job |
| `POST /api/uploads/images`, `/documents` | Raw body; `Content-Type`, `X-Note`, `X-Private` headers; queues the file |
| `POST /api/links/retry`, `/reprocess` | Put failed links back in the queue |
| `GET /api/links` | Queue and history |

The Telegram bot uses `queue_only`, so a saved link waits for the next run.

## Scheduling

Memex does not schedule itself. Runs start from the GUI's ingest button or from
an external scheduler (cron, Task Scheduler, a systemd timer). At the time of
writing the VM has no scheduler configured for memex, so links queued by the bot
stay queued until someone starts a run. Do not start a scheduled run while a GUI
job is in progress: the one-job-at-a-time guard lives inside the GUI only.

## Deployment on the homelab

Runs as `memex:local`, built from `~/Projects/memex` inside the shared compose
file `/docker/conf/compose.yaml` (no registry, Watchtower off). `scripts/deploy.sh`
pulls and runs `docker compose up -d --build memex`; the build takes a minute or
two because of the frontend and Chromium.

Configuration: `/docker/memex/config/config.toml` (read-only mount), data in
`/docker/memex/data` (state, media, docs, reports, `links.txt`), the Obsidian vault
mounted at `/vault`. Secrets and endpoint variables are in `/docker/conf/.env.memex`
and win over the file. The container runs as uid 1000 with a read-only root,
no capabilities and `no-new-privileges`.

## Tests

`python -m unittest discover -s tests -t .` (about 335 tests, a minute). The web
tests start the real server on an ephemeral port; LLM tests use
`httpx.MockTransport`. The frontend is type-checked with `npm run typecheck`.
