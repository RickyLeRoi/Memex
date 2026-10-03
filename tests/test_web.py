import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from digest.config import Config
from digest.state import State
from digest.web.server import create_server
from digest.web.service import Service


def make_service(tmp: Path) -> Service:
    cfg = Config(data_dir=str(tmp / "data"), reports_dir=str(tmp / "reports"), base_dir=tmp)
    return Service(cfg, tmp / "config.toml")


def seed_links(service: Service) -> None:
    fixtures = [
        ("https://a.example.com/pasta", "Pasta", ["ricette", "progetto-x"]),
        ("https://b.example.com/torta", "Torta", ["ricette"]),
        ("https://c.example.org/spec", "Spec", ["progetto-x"]),
    ]
    with State(service.cfg.data_path / "state.sqlite") as state:
        for url, title, tags in fixtures:
            state.add_link(url)
            state.link_done(url, title, {"title": title, "tags": tags})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self._tmp.name))
        seed_links(self.service)

    def tearDown(self):
        self._tmp.cleanup()

    def edges_of(self, graph: dict, kind: str) -> list[dict]:
        return [e for e in graph["edges"] if e["type"] == kind]

    def test_no_tag_edges_until_tags_are_curated(self):
        graph = self.service.graph()
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(self.edges_of(graph, "tag"), [])

    def test_only_curated_tags_create_edges(self):
        self.service.set_link_tags(["#Progetto-X"])
        edges = self.edges_of(self.service.graph(), "tag")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["tags"], ["progetto-x"])

    def test_generic_tag_never_links_unless_listed(self):
        self.service.set_link_tags(["progetto-x"])
        linked = {e["source"] for e in self.edges_of(self.service.graph(), "tag")}
        self.assertNotIn("https://b.example.com/torta", linked)

    def test_domain_edges_are_opt_in(self):
        self.assertEqual(self.edges_of(self.service.graph(), "domain"), [])
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://a.example.com/altro")
            state.link_done("https://a.example.com/altro", "Altro", {"tags": []})
        self.assertEqual(len(self.edges_of(self.service.graph(include_domain=True), "domain")), 1)

    def test_items_attach_to_their_doc_by_ref_url(self):
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.save_items("run1", [{"source": "mail", "kind": "task", "title": "Reply", "ref_url": "https://m/1"}])
        graph = self.service.graph()
        self.assertEqual(len(self.edges_of(graph, "ref")), 1)
        self.assertIn("https://m/1", {n["id"] for n in graph["nodes"]})

    def test_stats_counts_documents_and_available_tags(self):
        self.assertEqual(self.service.stats()["totals"]["documents"], 3)
        available = {t["tag"]: t["count"] for t in self.service.link_tags()["available"]}
        self.assertEqual(available["ricette"], 2)

    def test_add_links_skips_invalid_and_duplicates(self):
        added = self.service.add_links([
            {"url": "https://new.example.com", "note": "n"}, {"url": "not-a-url"},
            {"url": "https://new.example.com"},
        ])
        self.assertEqual(added, 1)

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.start_ingest(["nope"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.service = make_service(tmp)
        seed_links(self.service)
        self.server = create_server(self.service, port=0, dist_dir=tmp / "missing-dist")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        raw = json.dumps(body) if body is not None else None
        merged = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, raw, merged)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else None)

    def test_stats_route(self):
        status, data = self.call("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["totals"]["documents"], 3)

    def test_put_tags_then_graph_has_edges(self):
        self.assertEqual(self.call("PUT", "/api/graph/tags", {"tags": ["progetto-x"]})[0], 200)
        _, graph = self.call("GET", "/api/graph")
        self.assertEqual(len([e for e in graph["edges"] if e["type"] == "tag"]), 1)

    def test_post_requires_json_content_type(self):
        status, _ = self.call("POST", "/api/ingest/links", {"links": []}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_foreign_host_header_is_rejected(self):
        status, _ = self.call("GET", "/api/stats", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)

    def test_ingest_unknown_family_is_404(self):
        self.assertEqual(self.call("POST", "/api/ingest/nope", {})[0], 404)

    def test_delete_flow_requires_the_token_from_the_impact(self):
        url = "https://a.example.com/pasta"
        status, impact = self.call("GET", "/api/documents/impact?kind=link&id=" + url.replace(":", "%3A").replace("/", "%2F"))
        self.assertEqual(status, 200)
        self.assertEqual(impact["db"]["links"], 1)
        self.assertEqual(self.call("DELETE", "/api/documents", {"kind": "link", "id": url, "token": "wrong"})[0], 409)
        status, result = self.call("DELETE", "/api/documents", {"kind": "link", "id": url, "token": impact["token"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.call("GET", "/api/stats")[1]["totals"]["documents"], 2)

    def test_delete_validates_input(self):
        self.assertEqual(self.call("DELETE", "/api/documents", {"kind": "link"})[0], 400)
        self.assertEqual(self.call("GET", "/api/documents/impact?kind=item&id=9999")[0], 404)
        self.assertEqual(self.call("GET", "/api/documents/impact?kind=bogus&id=x")[0], 400)

    def test_tag_editing_route(self):
        url = "https://a.example.com/pasta"
        status, data = self.call("POST", "/api/tags", {"kind": "link", "id": url, "add": ["Cena"], "remove": ["ricette"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["tags"], ["progetto-x", "cena"])
        self.assertEqual(data["tag_origin"]["cena"], "manual")
        status, _ = self.call("POST", "/api/tags", {"kind": "link", "id": url, "remove": ["progetto-x", "cena"]})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("POST", "/api/tags", {"kind": "item", "id": "999", "add": ["x"]})[0], 404)
        self.assertEqual(self.call("POST", "/api/tags", {"kind": "link"})[0], 400)

    def test_missing_frontend_build_reports_503(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/")
        self.assertEqual(conn.getresponse().status, 503)
        conn.close()


if __name__ == "__main__":
    unittest.main()
