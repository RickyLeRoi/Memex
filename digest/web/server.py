from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..media import ImageRejected
from ..purge import NotFound, PurgeError, StaleImpact
from ..sources.documents import PdfError
from .service import Service

log = logging.getLogger("digest.web")
MAX_BODY_BYTES = 1_000_000
# 20261002 ** RG #docker the built frontend can live elsewhere (installed package, container image)
DIST_DIR = Path(os.environ.get("DIGEST_FRONTEND_DIST") or Path(__file__).resolve().parents[2] / "frontend" / "dist")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{12})$")
INGEST_PATH = re.compile(r"^/api/ingest/(links|chat|mail|tickets)$")
AREA_PATH = re.compile(r"^/api/areas/([a-z0-9-]{1,40})(?:/(approve|reject|vault-purge))?$")
NOT_AN_AREA_ROUTE = object()


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def make_handler(service: Service, dist_dir: Path):
    from .service import FAMILIES

    class Handler(BaseHTTPRequestHandler):
        server_version = "memex"

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload, status: int = 200, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ApiError(415, "Content-Type must be application/json")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "body too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ApiError(400, "invalid JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "JSON object expected")
            return data

        def _check_host(self) -> None:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOCAL_HOSTS | {h.lower() for h in service.cfg.server.allowed_hosts}:
                raise ApiError(403, "forbidden host")

        def _check_auth(self) -> None:
            """Optional access token (HTTP Basic, any user name). Constant-time comparison."""
            token = service.cfg.server.token
            if not token:
                return
            sent = ""
            scheme, _, encoded = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "basic":
                try:
                    sent = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")[2]
                except (ValueError, UnicodeDecodeError):
                    sent = ""
            if not hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8")):
                log.warning("rejected request without a valid token from %s", self.address_string())
                raise ApiError(401, "authentication required", {"WWW-Authenticate": 'Basic realm="Memex"'})

        def _dispatch(self, method: str) -> None:
            try:
                self._check_host()
                url = urlparse(self.path)
                if method == "GET" and url.path == "/healthz":  # 20261002 ++ RG #docker liveness, no auth, no data
                    self._send_json({"ok": True})
                    return
                self._check_auth()
                if method == "GET" and url.path.startswith("/api/media/"):
                    self._serve_media(url.path.removeprefix("/api/media/"))
                elif method == "GET" and url.path.startswith("/api/docs/"):
                    self._serve_doc(url.path.removeprefix("/api/docs/"))
                elif url.path.startswith("/api/"):
                    self._send_json(self._route(method, url.path, parse_qs(url.query)))
                elif method == "GET":
                    self._serve_static(url.path)
                else:
                    raise ApiError(405, "method not allowed")
            except ApiError as e:
                self._send_json({"error": str(e)}, e.status, e.headers)
            except Exception:  # noqa: BLE001 - never leak a stack trace to the client
                log.exception("unhandled error on %s %s", method, self.path)
                self._send_json({"error": "internal error"}, 500)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _route(self, method: str, path: str, query: dict[str, list[str]]):
            flag = lambda name: (query.get(name) or ["0"])[0] in ("1", "true")  # noqa: E731
            if method == "GET":
                if path == "/api/stats":
                    return service.stats()
                if path == "/api/sources":
                    return service.sources_status()
                if path == "/api/links":
                    return service.list_links()
                if path == "/api/links/detail":
                    url = (query.get("url") or [""])[0]
                    return self._purge(lambda: service.link_detail(url))
                if path == "/api/graph":
                    return service.graph(include_domain=flag("domain"))
                if path == "/api/graph/tags":
                    return service.link_tags()
                if (m := JOB_PATH.match(path)):
                    job = service.job(m.group(1))
                    if job is None:
                        raise ApiError(404, "job not found")
                    return job
                if path == "/api/jobs":
                    return {"running": (j.as_dict() if (j := service.running_job()) else None)}
                if path == "/api/documents/impact":
                    kind, ident = (query.get("kind") or [""])[0], (query.get("id") or [""])[0]
                    return self._purge(lambda: service.delete_impact(kind, ident))
            if (area_route := self._area_route(method, path)) is not NOT_AN_AREA_ROUTE:
                return area_route
            if method == "DELETE" and path == "/api/documents":
                body = self._read_json()
                kind, ident, token = (str(body.get(k) or "") for k in ("kind", "id", "token"))
                if not (kind and ident and token):
                    raise ApiError(400, "kind, id and token are required")
                return self._purge(lambda: service.delete_document(kind, ident, token))
            if method == "PUT" and path == "/api/graph/tags":
                tags = self._read_json().get("tags")
                if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                    raise ApiError(400, "tags must be a list of strings")
                return {"linking": service.set_link_tags(tags)}
            if method == "POST":
                if path == "/api/uploads/images":
                    return self._upload("image/", service.cfg.links.max_screenshot_bytes, service.queue_screenshot)
                if path == "/api/uploads/documents":
                    return self._upload("application/pdf", service.cfg.links.max_pdf_bytes, service.queue_document)
                if path == "/api/tags":
                    body = self._read_json()
                    kind, ident = str(body.get("kind") or ""), str(body.get("id") or "")
                    add, remove = body.get("add") or [], body.get("remove") or []
                    if not (kind and ident) or not all(isinstance(x, str) for x in [*add, *remove]):
                        raise ApiError(400, "kind, id and string lists add/remove are required")
                    return self._purge(lambda: service.edit_tags(kind, ident, add, remove))
                if path == "/api/links/reprocess":
                    url = str(self._read_json().get("url") or "")
                    if not url:
                        raise ApiError(400, "url is required")
                    self._purge(lambda: service.reprocess_link(url))
                    return {"ok": True}
                if path == "/api/links/retry":
                    service.retry_failed_links()
                    return {"ok": True}
                if (m := INGEST_PATH.match(path)):
                    return self._ingest(m.group(1), self._read_json())
            raise ApiError(404, "not found")

        def _area_route(self, method: str, path: str):
            if path == "/api/areas":
                if method == "GET":
                    return service.areas()
                if method == "POST":
                    body = self._read_json()
                    return self._purge(lambda: service.create_area(
                        str(body.get("label") or ""), str(body.get("description") or ""), str(body.get("color") or "")))
            if path == "/api/areas/assign" and method == "POST":
                body = self._read_json()
                return self._purge(lambda: service.assign_area(
                    str(body.get("kind") or ""), str(body.get("id") or ""), str(body.get("area") or "")))
            found = AREA_PATH.match(path)
            if not found:
                return NOT_AN_AREA_ROUTE
            name, action = found.groups()
            if action is None and method == "PUT":
                changes = self._read_json()
                return self._purge(lambda: service.update_area(name, changes))
            if action is None and method == "DELETE":
                return self._purge(lambda: service.delete_area(name))
            if action == "approve" and method == "POST":
                changes = self._read_json()
                return {"job": self._purge(lambda: service.approve_area(name, changes)).id}
            if action == "reject" and method == "POST":
                self._purge(lambda: service.reject_area(name))
                return {"ok": True}
            if action == "vault-purge" and method == "POST":
                return self._purge(lambda: service.clean_vault_for_area(name))
            return NOT_AN_AREA_ROUTE

        def _upload(self, content_type_prefix: str, limit: int, queue):
            """Raw file body. A non-simple Content-Type forces a CORS preflight, so other sites cannot post here."""
            if not (self.headers.get("Content-Type") or "").startswith(content_type_prefix):
                raise ApiError(415, f"Content-Type must start with {content_type_prefix}")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "invalid Content-Length") from None
            if length <= 0:
                raise ApiError(400, "empty body")
            if length > limit:
                self.close_connection = True  # the body is not read: do not reuse the connection
                raise ApiError(413, "file too large")
            data = self.rfile.read(length)
            try:
                return queue(data, unquote(self.headers.get("X-Note") or ""))
            except (ImageRejected, PdfError) as e:
                raise ApiError(400, str(e)) from None

        def _serve_doc(self, name: str) -> None:
            body = service.doc_file(name)
            if body is None:
                raise ApiError(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')  # download, never rendered inline
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _purge(action):
            try:
                return action()
            except NotFound as e:
                raise ApiError(404, str(e)) from None
            except StaleImpact as e:
                raise ApiError(409, str(e)) from None
            except RuntimeError as e:
                raise ApiError(409, str(e)) from None
            except (PurgeError, ValueError) as e:
                raise ApiError(400, str(e)) from None

        def _ingest(self, family: str, body: dict):
            if family == "links":
                links = body.get("links")
                if not isinstance(links, list):
                    raise ApiError(400, "links must be a list")
                added = service.add_links([x for x in links if isinstance(x, dict)])
                sources = ["links"]
            else:
                requested = body.get("sources") or FAMILIES[family]
                sources = [s for s in requested if s in FAMILIES[family]]
                added = 0
                if not sources:
                    raise ApiError(400, f"sources must be a subset of {FAMILIES[family]}")
            try:
                job = service.start_ingest(sources)
            except RuntimeError as e:
                raise ApiError(409, str(e)) from None
            except ValueError as e:
                raise ApiError(400, str(e)) from None
            return {"job": job.id, "added": added}

        def _serve_media(self, name: str) -> None:
            found = service.media_file(name)
            if found is None:
                raise ApiError(404, "not found")
            body, content_type = found
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("Cache-Control", "no-store")  # a deleted image must not survive in the browser cache
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, path: str) -> None:
            if not dist_dir.is_dir():
                raise ApiError(503, "frontend not built: run `npm install && npm run build` in ./frontend")
            relative = path.lstrip("/") or "index.html"
            target = (dist_dir / relative).resolve()
            if dist_dir.resolve() not in target.parents or not target.is_file():
                target = dist_dir / "index.html"  # SPA fallback
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if target.suffix == ".html":
                self.send_header("Cache-Control", "no-cache")  # 20261002 ++ RG #gui hashed assets stay cacheable
            self.end_headers()
            self.wfile.write(body)

    return Handler


def create_server(service: Service, host: str = "127.0.0.1", port: int = 8765,
                  dist_dir: Path = DIST_DIR) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, dist_dir))
