# tests/e2e/fakes.py
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ARTICLE_PARAGRAPHS = [
    "Il refactoring migliora la struttura del codice senza cambiarne il comportamento osservabile da fuori.",
    "Si parte sempre dai test: senza una rete di sicurezza ogni modifica diventa una scommessa sulla fortuna.",
    "Si procede a piccoli passi, con commit frequenti, così che ogni regressione sia facile da isolare.",
    "Estrarre funzioni, rinominare variabili e ridurre le dipendenze sono le mosse più comuni e più efficaci.",
    "Un buon refactoring lascia il codice più semplice da leggere per chi arriverà dopo di te, anche se sei tu.",
]
ARTICLE_HTML = (
    "<html><head><title>Guida al refactoring</title>"
    '<meta property="og:title" content="Guida al refactoring"></head><body><main><article>'
    "<h1>Guida al refactoring</h1>" + "".join(f"<p>{p}</p>" for p in ARTICLE_PARAGRAPHS) + "</article></main></body></html>"
)

RECIPE_HTML = """<html><head><title>Torta soffice</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Recipe","name":"Torta soffice",
"recipeCuisine":"italiana","recipeYield":"8 porzioni","totalTime":"PT45M",
"recipeIngredient":["200 g farina","3 uova","150 g zucchero"],
"recipeInstructions":[{"@type":"HowToStep","text":"Sbatti le uova con lo zucchero."},
{"@type":"HowToStep","text":"Aggiungi la farina e inforna a 180 gradi."}]}</script></head>
<body><h1>Torta soffice</h1><p>La ricetta della torta soffice di una volta.</p></body></html>"""

FAKE_LLM_ANALYSIS = {
    "is_recipe": False,
    "summary": "Riassunto generato dal modello finto.",
    "key_points": ["punto uno", "punto due"],
    "tags": ["refactoring", "codice"],
    "actions": [],
    "worth_it": "alta",
}


class _Server:
    def __init__(self, handler_factory):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeSite(_Server):
    """Serves canned pages; /blocked answers 403 like a bot wall, /empty has nothing worth extracting."""

    PAGES = {
        "/article": (200, ARTICLE_HTML),
        "/recipe": (200, RECIPE_HTML),
        "/blocked": (403, "Forbidden"),
        "/empty": (200, "<html><body></body></html>"),
    }

    def __init__(self):
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                site.hits.append(self.path)
                status, body = site.PAGES.get(self.path, (404, "not found"))
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        super().__init__(Handler)


class FakeLLM(_Server):
    """OpenAI-compatible /chat/completions. The title echoes the 'Titolo:' header the fetcher puts in the prompt."""

    def __init__(self):
        self.requests: list[dict] = []
        llm = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                llm.requests.append(body)
                payload = json.dumps({"choices": [{"message": {"content": json.dumps(llm.answer(body))}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        super().__init__(Handler)

    @staticmethod
    def answer(body: dict) -> dict:
        user = str(body["messages"][-1]["content"])
        title = next((ln.removeprefix("Titolo: ") for ln in user.splitlines() if ln.startswith("Titolo: ")), "")
        return {**FAKE_LLM_ANALYSIS, "title": title, "highlights": ["Un link da leggere"], "ingredients": [], "steps": []}


def write_config(root: Path, llm_url: str) -> Path:
    config = root / "config.toml"
    config.write_text(
        f'data_dir = "data"\nreports_dir = "reports"\n\n'
        f'[llm]\nbase_url = "{llm_url}"\nmodel = "fake"\nmax_tokens = 512\ntimeout = 30\n\n'
        f'[links]\nfile = "links.txt"\nuse_ytdlp = false\nuse_playwright = false\nfetch_images = false\n'
        f'max_attempts = 3\n',
        encoding="utf-8",
    )
    return config


def run_digest(config: Path, *arguments: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "NO_COLOR": "1"}
    return subprocess.run(
        [sys.executable, "-m", "digest", "-c", str(config), *arguments],
        cwd=config.parent, env=env, capture_output=True, text=True, timeout=120,
    )
