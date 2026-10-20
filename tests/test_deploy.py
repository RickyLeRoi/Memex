import base64
import contextlib
import http.client
import importlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from digest import __main__ as cli
from digest.config import ConfigError, load_config
from digest.web import server as server_module
from digest.web.server import create_server
from tests.test_web import make_service

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "s3cret-token"


def basic(password: str, user: str = "anyone") -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


class ServerCase(unittest.TestCase):
    token = ""
    allowed_hosts: list[str] = []

    def setUp(self):
        logger = logging.getLogger("digest.web")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service = make_service(self.tmp)
        self.service.cfg.server.token = self.token
        self.service.cfg.server.allowed_hosts = list(self.allowed_hosts)
        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
        self.server = create_server(self.service, port=0, dist_dir=dist)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def get(self, path: str, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body, headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data


class HostAllowListTests(ServerCase):
    allowed_hosts = ["192.168.1.20", "Digest.LAN"]

    def test_configured_hosts_are_accepted_case_insensitively_and_others_are_not(self):
        for host in ("192.168.1.20:8765", "digest.lan", "localhost:8765", "127.0.0.1"):
            self.assertEqual(self.get("/api/stats", {"Host": host})[0].status, 200, host)
        for host in ("evil.example.com", "192.168.1.21", "digest.lan.evil.com", ""):
            self.assertEqual(self.get("/api/stats", {"Host": host})[0].status, 403, host)


class TokenTests(ServerCase):
    token = TOKEN

    def test_every_route_needs_the_token_and_asks_the_browser_for_it(self):
        for path in ("/", "/api/stats", "/api/links", "/api/graph", "/api/areas", "/api/media/" + "0" * 40 + ".png",
                     "/api/docs/" + "0" * 40 + ".pdf"):
            resp, _ = self.get(path)
            self.assertEqual(resp.status, 401, path)
            self.assertIn("Basic", resp.getheader("WWW-Authenticate"))

    def test_writes_are_protected_too(self):
        body = json.dumps({"links": []}).encode()
        for method, path in (("POST", "/api/ingest/links"), ("DELETE", "/api/documents"), ("PUT", "/api/graph/tags"),
                             ("POST", "/api/uploads/images"), ("POST", "/api/areas")):
            self.assertEqual(self.get(path, {"Content-Type": "application/json"}, method, body)[0].status, 401, path)

    def test_the_right_token_with_any_user_name_gets_in(self):
        for user in ("anyone", "", "riccardo"):
            self.assertEqual(self.get("/api/stats", basic(TOKEN, user))[0].status, 200, user)
        resp, body = self.get("/", basic(TOKEN))
        self.assertEqual((resp.status, body), (200, b"<html>app</html>"))

    def test_wrong_empty_partial_or_malformed_credentials_are_refused(self):
        attempts = [basic("wrong"), basic(""), basic(TOKEN[:-1]), basic(TOKEN + "x"), basic(TOKEN.upper()),
                    {"Authorization": "Basic !!!not-base64!!!"}, {"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()},
                    {"Authorization": "Bearer " + TOKEN}, {"Authorization": TOKEN}, {"Authorization": "Basic"}]
        for headers in attempts:
            self.assertEqual(self.get("/api/stats", headers)[0].status, 401, headers)

    def test_the_token_is_never_echoed_back(self):
        resp, body = self.get("/api/stats")
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(TOKEN, str(resp.getheaders()))

    def test_healthz_needs_no_token_and_returns_nothing_sensitive(self):
        resp, body = self.get("/healthz")
        self.assertEqual((resp.status, json.loads(body)), (200, {"ok": True}))

    def test_host_check_still_applies_with_a_valid_token(self):
        headers = {**basic(TOKEN), "Host": "evil.example.com"}
        self.assertEqual(self.get("/api/stats", headers)[0].status, 403)


class OpenByDefaultTests(ServerCase):
    def test_without_a_token_nothing_is_asked_and_healthz_works(self):
        self.assertEqual(self.get("/api/stats")[0].status, 200)
        self.assertEqual(self.get("/healthz")[0].status, 200)


class ServeGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(server=SimpleNamespace(token="", allowed_hosts=[]))
        self.args = SimpleNamespace(host="0.0.0.0", port=8765, config=str(Path(self._tmp.name) / "config.toml"))

    def tearDown(self):
        self._tmp.cleanup()

    def serve(self, host: str, token: str = "", allow: str | None = None) -> int:
        self.cfg.server.token, self.args.host = token, host
        env = {"DIGEST_ALLOW_UNAUTHENTICATED": allow} if allow is not None else {}
        fake = mock.MagicMock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("digest.web.server.create_server", return_value=fake), mock.patch("digest.web.service.Service"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if allow is None:
                os.environ.pop("DIGEST_ALLOW_UNAUTHENTICATED", None)
            return cli.cmd_serve(self.cfg, self.args)

    def test_refuses_to_listen_on_the_network_without_a_token(self):
        for host in ("0.0.0.0", "192.168.1.20", "::"):
            self.assertEqual(self.serve(host), 2, host)

    def test_the_escape_hatch_must_be_exactly_1(self):
        self.assertEqual(self.serve("0.0.0.0", allow="0"), 2)
        self.assertEqual(self.serve("0.0.0.0", allow="yes"), 2)
        self.assertEqual(self.serve("0.0.0.0", allow="1"), 0)

    def test_a_token_allows_it(self):
        self.assertEqual(self.serve("0.0.0.0", token=TOKEN), 0)

    def test_loopback_never_needs_anything(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(self.serve(host), 0, host)


class ConfigEnvTests(unittest.TestCase):
    def load(self, env: dict[str, str], toml: str = ""):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(toml, encoding="utf-8")
            with mock.patch.dict(os.environ, env):
                return load_config(path)

    def test_defaults_are_open_to_localhost_only(self):
        cfg = self.load({"DIGEST_GUI_TOKEN": "", "DIGEST_ALLOWED_HOSTS": ""})
        self.assertEqual((cfg.server.token, cfg.server.allowed_hosts), ("", []))

    def test_environment_provides_the_token_and_extra_hosts(self):
        cfg = self.load({"DIGEST_GUI_TOKEN": "abc", "DIGEST_ALLOWED_HOSTS": " 192.168.1.20 , digest.lan,,"},
                        '[server]\nallowed_hosts = ["from-file.lan"]\n')
        self.assertEqual(cfg.server.token, "abc")
        self.assertEqual(cfg.server.allowed_hosts, ["from-file.lan", "192.168.1.20", "digest.lan"])

    def test_unknown_server_keys_are_rejected(self):
        with self.assertRaises(ConfigError):
            self.load({}, "[server]\nport = 1\n")

    def test_the_docker_config_template_loads_and_uses_container_paths(self):
        with mock.patch.dict(os.environ, {"DIGEST_GUI_TOKEN": ""}):
            cfg = load_config(ROOT / "docker" / "config.docker.toml")
        self.assertEqual((cfg.data_dir, cfg.reports_dir, cfg.links.file), ("/data", "/data/reports", "/data/links.txt"))
        self.assertIn("host.docker.internal", cfg.llm.base_url)
        self.assertFalse(any([cfg.mail.enabled, cfg.teams.enabled, cfg.slack.enabled, cfg.gmail.enabled, cfg.jira.enabled]))

    def test_the_frontend_location_can_be_overridden(self):
        try:
            with mock.patch.dict(os.environ, {"DIGEST_FRONTEND_DIST": "/app/frontend/dist"}):
                importlib.reload(server_module)
                self.assertEqual(server_module.DIST_DIR, Path("/app/frontend/dist"))
        finally:
            importlib.reload(server_module)


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class EntrypointTests(unittest.TestCase):
    """The container entrypoint, run with a stand-in `python` that only prints what it receives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "python"
        fake.write_text("#!/bin/sh\necho \"$@\"\n", encoding="utf-8", newline="\n")
        fake.chmod(0o755)
        self.config = self.tmp / "config.toml"
        self.config.write_text("", encoding="utf-8")
        self.env = {**os.environ, "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
                    "DIGEST_CONFIG": self.config.as_posix()}

    def tearDown(self):
        self._tmp.cleanup()

    def run_entrypoint(self, *args: str, env: dict | None = None):
        return subprocess.run(["sh", (ROOT / "docker" / "entrypoint.sh").as_posix(), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=20)

    def test_default_is_serve_on_all_interfaces_inside_the_container(self):
        out = self.run_entrypoint().stdout.strip()
        self.assertEqual(out, f"-m digest -c {self.config.as_posix()} serve --host 0.0.0.0")

    def test_serve_passes_extra_arguments_through(self):
        out = self.run_entrypoint("serve", "--port", "9000").stdout.strip()
        self.assertTrue(out.endswith("serve --host 0.0.0.0 --port 9000"), out)

    def test_any_other_command_is_a_plain_digest_command(self):
        self.assertTrue(self.run_entrypoint("run", "--sources", "links").stdout.strip().endswith("run --sources links"))
        self.assertTrue(self.run_entrypoint("login").stdout.strip().endswith("login"))

    def test_a_missing_config_stops_with_instructions(self):
        env = {**self.env, "DIGEST_CONFIG": (self.tmp / "missing.toml").as_posix()}
        result = self.run_entrypoint(env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config.docker.toml", result.stderr)
        self.assertEqual(result.stdout, "")


class RepositoryHygieneTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_secrets_and_local_data_are_ignored_by_git_and_docker(self):
        for pattern in ("config.toml", "links.txt", "reports/", "data/", ".env", "frontend/node_modules/", "config/"):
            self.assertIn(pattern, self.read(".gitignore").splitlines(), pattern)
        for pattern in ("config.toml", "links.txt", "reports/", ".env", "frontend/node_modules", ".git"):
            self.assertIn(pattern, self.read(".dockerignore").splitlines(), pattern)

    def test_shell_scripts_keep_unix_line_endings(self):
        self.assertIn("*.sh text eol=lf", self.read(".gitattributes"))
        self.assertNotIn(b"\r\n", (ROOT / "docker" / "entrypoint.sh").read_bytes())

    def test_compose_publishes_on_loopback_only_by_default(self):
        compose = self.read("docker-compose.yml")
        self.assertIn('"127.0.0.1:8765:8765"', compose)
        self.assertNotIn('"8765:8765"', compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges", compose)

    def test_the_image_runs_unprivileged_and_has_a_healthcheck(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("USER digest", dockerfile)
        self.assertIn("/healthz", dockerfile)
        self.assertNotIn("USER root", dockerfile)

    def test_no_real_secrets_or_personal_addresses_are_committed(self):
        # the SHAPE of real credentials: comments such as "xoxp-..." that only describe a format must not trigger it
        suspicious = [re.compile(p) for p in (r"xox[abpr]-\d{6,}-", r"ghp_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
                                              r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}", r"BEGIN (RSA |EC )?PRIVATE KEY",
                                              r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.")]
        tracked = [p for p in ROOT.rglob("*") if p.is_file() and not any(
            part in (".venv", "node_modules", "dist", "reports", "data", ".git", "__pycache__", ".claude") or part.endswith(".egg-info")
            for part in p.relative_to(ROOT).parts) and p.name not in ("config.toml", "links.txt", ".env")
            and p.suffix in (".py", ".md", ".toml", ".yml", ".yaml", ".sh", ".ts", ".tsx", ".json", ".css", ".html", "")]
        offenders = []
        for path in tracked:
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders += [f"{path.relative_to(ROOT)}: {pattern.pattern}" for pattern in suspicious if pattern.search(text)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
