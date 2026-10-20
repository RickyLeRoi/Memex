import http.client
import ipaddress
import json
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx

from digest.config import Config, LinksConfig, ObsidianConfig
from digest.media import (ImageRejected, UnsafeUrl, check_public_url, cover_candidates, fetch_image, store_image,
                          try_fetch_image)
from digest.obsidian import write_obsidian
from digest.purge import Purger
from digest.report import write_reports
from digest.sources.links import LinkFetcher, fetch_links, og_meta
from digest.state import State
from digest.web.server import create_server
from tests.test_web import make_service

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"y" * 64
PUBLIC_IP = "93.184.216.34"


@contextmanager
def dns(*addresses: str):
    def fake(host, port, type=0, **_):
        try:
            ipaddress.ip_address(host)
            resolved = (host,)
        except ValueError:
            resolved = addresses
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in resolved]

    with mock.patch("digest.media.socket.getaddrinfo", side_effect=fake):
        yield


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def image_response(body: bytes = PNG, content_type: str = "image/png", **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": content_type, **headers})


class SsrfTests(unittest.TestCase):
    def test_non_public_or_odd_urls_are_refused(self):
        refused = [
            "http://example.com/a.png", "ftp://example.com/a.png", "https://user:pw@example.com/a.png",
            "https://example.com:8443/a.png", "https://127.0.0.1/a.png", "https://10.0.0.5/a.png",
            "https://192.168.1.10/a.png", "https://169.254.169.254/latest/meta-data", "https://[::1]/a.png",
            "https://[::ffff:127.0.0.1]/a.png", "https://0.0.0.0/a.png", "https://100.64.0.1/a.png",
            "https://224.0.0.1/a.png", "https:///a.png", "not a url",
        ]
        for url in refused:
            with self.subTest(url=url), self.assertRaises(UnsafeUrl):
                check_public_url(url)

    def test_hostname_resolving_to_a_private_address_is_refused(self):
        with dns("192.168.1.10"), self.assertRaises(UnsafeUrl):
            check_public_url("https://innocent.example.com/a.png")

    def test_one_private_address_among_public_ones_is_enough_to_refuse(self):
        with dns(PUBLIC_IP, "10.0.0.1"), self.assertRaises(UnsafeUrl):
            check_public_url("https://mixed.example.com/a.png")

    def test_unresolvable_host_is_refused(self):
        with mock.patch("digest.media.socket.getaddrinfo", side_effect=socket.gaierror), self.assertRaises(UnsafeUrl):
            check_public_url("https://nope.example.com/a.png")

    def test_public_https_is_accepted(self):
        with dns(PUBLIC_IP):
            check_public_url("https://example.com/a.png")


class FetchTests(unittest.TestCase):
    def fetch(self, handler, max_bytes: int = 1000, url: str = "https://img.example.com/a.png"):
        with dns(PUBLIC_IP):
            return fetch_image(client(handler), url, max_bytes)

    def test_valid_image_is_returned_with_the_extension_from_its_bytes(self):
        data, ext = self.fetch(lambda r: image_response(JPEG, "image/png"))  # the lying Content-Type does not matter
        self.assertEqual((data, ext), (JPEG, "jpg"))

    def test_not_an_image_content_type_is_rejected(self):
        for content_type in ("text/html", "application/octet-stream", "image/svg+xml", ""):
            with self.subTest(content_type=content_type), self.assertRaises(ImageRejected):
                self.fetch(lambda r, ct=content_type: image_response(b"<svg onload=alert(1)>", ct))

    def test_image_content_type_with_non_image_bytes_is_rejected(self):
        with self.assertRaises(ImageRejected):
            self.fetch(lambda r: image_response(b"<html>definitely not a png</html>"))

    def test_declared_oversize_is_rejected(self):
        with self.assertRaises(ImageRejected):
            self.fetch(lambda r: image_response(PNG, **{"content-length": "999999"}), max_bytes=1000)

    def test_oversize_is_rejected_even_without_content_length(self):
        def handler(request):
            return httpx.Response(200, content=PNG + b"z" * 5000, headers={"content-type": "image/png"})

        with self.assertRaises(ImageRejected):
            self.fetch(handler, max_bytes=1000)

    def test_redirect_to_a_private_address_is_blocked_at_the_hop(self):
        def handler(request):
            if request.url.host == "img.example.com":
                return httpx.Response(302, headers={"location": "https://192.168.1.10/secret.png"})
            return image_response()

        with self.assertRaises(UnsafeUrl):
            self.fetch(handler)

    def test_redirect_to_plain_http_is_blocked(self):
        def handler(request):
            return httpx.Response(302, headers={"location": "http://img.example.com/a.png"})

        with self.assertRaises(UnsafeUrl):
            self.fetch(handler)

    def test_public_redirect_is_followed(self):
        def handler(request):
            if request.url.path == "/a.png":
                return httpx.Response(301, headers={"location": "/b.png"})
            return image_response()

        self.assertEqual(self.fetch(handler)[1], "png")

    def test_redirect_loop_gives_up(self):
        with self.assertRaises(ImageRejected):
            self.fetch(lambda r: httpx.Response(302, headers={"location": "/again.png"}))

    def test_try_fetch_returns_none_instead_of_raising(self):
        with dns(PUBLIC_IP):
            self.assertIsNone(try_fetch_image(client(lambda r: httpx.Response(404)), "https://i.example.com/x.png", 1000))
        self.assertIsNone(try_fetch_image(client(lambda r: image_response()), "https://127.0.0.1/x.png", 1000))


class StoreTests(unittest.TestCase):
    def test_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            first = store_image(media, PNG, "png")
            second = store_image(media, PNG, "png")
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{40}\.png$")
            self.assertEqual([p.name for p in media.iterdir()], [first])
            self.assertNotEqual(store_image(media, JPEG, "jpg"), first)


class CoverCandidateTests(unittest.TestCase):
    HTML = """<html><head>
      <meta property="og:image" content="/og.jpg"><meta name="twitter:image" content="https://cdn.example.com/tw.jpg">
      <script type="application/ld+json">{"@context":"https://schema.org","@graph":[
        {"@type":"Recipe","name":"Pasta","image":["https://cdn.example.com/recipe1.jpg","/recipe2.jpg"]}]}</script>
      <script type="application/ld+json">{ broken json</script>
    </head><body></body></html>"""

    def test_priority_is_jsonld_then_og_then_twitter_and_urls_are_absolute(self):
        out = cover_candidates(self.HTML, "https://site.example.com/page", og_meta(self.HTML))
        self.assertEqual(out, [
            "https://cdn.example.com/recipe1.jpg", "https://site.example.com/recipe2.jpg",
            "https://site.example.com/og.jpg", "https://cdn.example.com/tw.jpg",
        ])

    def test_object_form_and_duplicates(self):
        html = ('<script type="application/ld+json">{"image":{"@type":"ImageObject","url":"https://a.example.com/i.jpg"}}'
                '</script><meta property="og:image" content="https://a.example.com/i.jpg">')
        self.assertEqual(cover_candidates(html, "https://a.example.com/", og_meta(html)), ["https://a.example.com/i.jpg"])

    def test_no_image_means_no_candidates(self):
        self.assertEqual(cover_candidates("<html></html>", "https://a.example.com/", {}), [])


PAGE = """<html><head><title>Pasta</title><meta property="og:image" content="https://img.example.com/cover.png">
</head><body><article><p>%s</p></article></body></html>""" % ("A very tasty recipe with many words. " * 40)


class LinkIntegrationTests(unittest.TestCase):
    URL = "https://blog.example.com/pasta"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp,
                          links=LinksConfig(use_ytdlp=False))
        self.state = State(self.cfg.data_path / "state.sqlite")
        self.state.add_link(self.URL)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def http(self, image: httpx.Response | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "img.example.com":
                return image or image_response()
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

        return client(handler)

    def test_cover_is_stored_and_attached_to_the_doc(self):
        with dns(PUBLIC_IP):
            docs, errors = fetch_links(self.cfg, self.state, None, self.http())
        self.assertEqual(errors, [])
        name = docs[0].meta["image"]
        self.assertTrue((self.cfg.data_path / "media" / name).is_file())

    def test_unusable_image_does_not_fail_the_link(self):
        with dns(PUBLIC_IP):
            docs, errors = fetch_links(self.cfg, self.state, None, self.http(httpx.Response(404)))
        self.assertEqual(errors, [])
        self.assertIsNone(docs[0].meta["image"])

    def test_images_can_be_switched_off_and_dry_run_stores_nothing(self):
        with dns(PUBLIC_IP):
            docs, _ = fetch_links(self.cfg, self.state, None, self.http(), store_images=False)
        self.assertIsNone(docs[0].meta["image"])
        self.cfg.links.fetch_images = False
        with dns(PUBLIC_IP):
            docs, _ = fetch_links(self.cfg, self.state, None, self.http())
        self.assertIsNone(docs[0].meta["image"])
        self.assertFalse((self.cfg.data_path / "media").exists())

    def test_vision_download_goes_through_the_same_guard(self):
        class Vision:
            def __init__(self):
                self.calls = 0

            def describe_image(self, data, mime, prompt):
                self.calls += 1
                return "a plate of pasta"

        self.cfg.llm.vision_model = "v"
        vision = Vision()
        fetcher = LinkFetcher(self.cfg, vision, self.http())
        with dns(PUBLIC_IP):
            self.assertEqual(fetcher._describe_images(["https://img.example.com/c.png"]), ["a plate of pasta"])
        self.assertEqual(fetcher._describe_images(["https://127.0.0.1/c.png"]), [])
        self.assertEqual(vision.calls, 1)


def link(url: str, title: str, image: str | None) -> dict:
    return {"url": url, "platform": "web", "note": "", "title": title, "summary": f"About {title}",
            "key_points": [], "tags": ["t"], "actions": [], "worth_it": "media", "image": image}


class ImagePipelineCase(unittest.TestCase):
    A, B = "https://a.example.com/a", "https://b.example.com/b"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp,
                          obsidian=ObsidianConfig(enabled=True, vault=str(self.vault)))
        self.media = self.cfg.data_path / "media"
        self.image_a = store_image(self.media, PNG, "png")
        self.image_b = store_image(self.media, JPEG, "jpg")
        self.state = State(self.cfg.data_path / "state.sqlite")
        self.result = {"started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 2}, "items": [],
                       "errors": [], "highlights": [],
                       "links": [link(self.A, "Page A", self.image_a), link(self.B, "Page B", self.image_b)]}
        for ln in self.result["links"]:
            self.state.add_link(ln["url"])
            self.state.link_done(ln["url"], ln["title"], ln)
        write_reports(self.result, self.cfg.reports_path)
        write_obsidian(self.result, self.cfg)
        (self.tmp / "links.txt").write_text(f"{self.A}\n{self.B}\n", encoding="utf-8")
        self.links_dir = self.vault / "Digest" / "Link"

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def delete(self, url: str) -> dict:
        purger = Purger(self.cfg, self.state)
        return purger.execute("link", url, purger.plan("link", url).token)


class StorageAndVaultTests(ImagePipelineCase):
    def test_state_keeps_the_image_name_and_a_reprocess_without_image_keeps_it(self):
        self.assertEqual(self.state.link_image(self.A), self.image_a)
        self.state.link_done(self.A, "Page A", {"tags": ["t"], "image": None})
        self.assertEqual(self.state.link_image(self.A), self.image_a)

    def test_vault_note_embeds_a_copy_of_the_image(self):
        self.assertTrue((self.links_dir / "media" / self.image_a).is_file())
        note = (self.links_dir / "Page A.md").read_text(encoding="utf-8")
        self.assertIn(f"![[{self.image_a}]]", note)

    def test_copy_can_be_switched_off(self):
        shutil_target = self.links_dir / "media"
        for path in shutil_target.iterdir():
            path.unlink()
        self.cfg.obsidian.copy_media = False
        write_obsidian(self.result, self.cfg)
        self.assertEqual(list(shutil_target.iterdir()), [])
        self.assertNotIn("![[", (self.links_dir / "Page A.md").read_text(encoding="utf-8"))

    def test_a_name_that_is_not_ours_is_never_copied(self):
        evil = dict(self.result["links"][0], image="../../secret.txt", url="https://c.example.com/c", title="C")
        write_obsidian({**self.result, "links": [evil]}, self.cfg)
        self.assertNotIn("secret", " ".join(p.name for p in (self.links_dir / "media").iterdir()))


class ImageDeletionTests(ImagePipelineCase):
    def test_deleting_a_link_removes_its_image_and_the_vault_copy_but_not_the_others(self):
        result = self.delete(self.A)
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.media / self.image_a).exists())
        self.assertFalse((self.links_dir / "media" / self.image_a).exists())
        self.assertTrue((self.media / self.image_b).is_file())
        self.assertTrue((self.links_dir / "media" / self.image_b).is_file())

    def test_impact_lists_the_image_files(self):
        info = Purger(self.cfg, self.state).describe(Purger(self.cfg, self.state).plan("link", self.A))
        paths = " ".join(f["path"] for f in info["files"])
        self.assertIn(f"data:media/{self.image_a}", paths)
        self.assertIn(f"vault:Digest/Link/media/{self.image_a}", paths)

    def test_an_image_shared_by_two_links_survives_until_the_last_one_goes(self):
        shared = link("https://c.example.com/c", "Page C", self.image_a)
        self.state.add_link(shared["url"])
        self.state.link_done(shared["url"], "Page C", shared)
        write_obsidian({**self.result, "links": [shared]}, self.cfg)
        self.delete(self.A)
        self.assertTrue((self.media / self.image_a).is_file())
        self.assertTrue((self.links_dir / "media" / self.image_a).is_file())
        self.delete(shared["url"])
        self.assertFalse((self.media / self.image_a).exists())
        self.assertFalse((self.links_dir / "media" / self.image_a).exists())

    def test_a_poisoned_image_name_in_the_database_cannot_delete_other_files(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        self.state.db.execute("UPDATE links SET image='../../victim.txt' WHERE url=?", (self.A,))
        self.state.db.commit()
        self.delete(self.A)
        self.assertTrue(victim.exists())


class ServeMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service = make_service(self.tmp)
        self.name = store_image(self.service.cfg.data_path / "media", PNG, "png")
        self.server = create_server(self.service, port=0, dist_dir=self.tmp / "none")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def get(self, path: str, host: str = "127.0.0.1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_serves_a_stored_image_with_defensive_headers(self):
        resp, body = self.get(f"/api/media/{self.name}")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, PNG)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))

    def test_only_our_content_addressed_names_are_served(self):
        for path in ("/api/media/../../config.toml", "/api/media/..%2F..%2Fconfig.toml", "/api/media/x.png",
                     f"/api/media/{self.name[:-4]}.svg", f"/api/media/{'0' * 40}.png", "/api/media/"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0].status, 404)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.get(f"/api/media/{self.name}", host="evil.example.com")[0].status, 403)

    def test_listing_and_graph_expose_the_image_name(self):
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://l.example.com")
            state.link_done("https://l.example.com", "L", {"tags": ["t"], "image": self.name})
        self.assertEqual(self.service.list_links()[0]["image"], self.name)
        node = next(n for n in self.service.graph()["nodes"] if n["id"] == "https://l.example.com")
        self.assertEqual(node["image"], self.name)
        self.assertEqual(json.loads(json.dumps(node))["image"], self.name)


if __name__ == "__main__":
    unittest.main()
