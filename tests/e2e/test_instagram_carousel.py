# tests/e2e/test_instagram_carousel.py
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from digest.config import load_config
from digest.llm import LLM
from digest.sources.links import LinkFetcher, fetch_links
from digest.state import State

from .fakes import FakeLLM, write_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
POST_URL = "https://www.instagram.com/p/Dd3w1JdlH8b/?img_index=5"
SLIDES = [f"https://cdn.example.com/slide{n}.png" for n in (1, 2, 3)]


def instagram_world(request: httpx.Request) -> httpx.Response:
    if request.url.host == "cdn.example.com":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    return httpx.Response(403, text="login required")


class InstagramCarouselTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.llm_server = FakeLLM().__enter__()
        self.cfg = load_config(write_config(root, f"{self.llm_server.url}/v1"))
        self.cfg.llm.vision_model = "fake-vision"
        (root / "links.txt").write_text(f"{POST_URL}  idee torta\n", encoding="utf-8")
        self.state = State(self.cfg.data_path / "state.sqlite")
        self.http = httpx.Client(transport=httpx.MockTransport(instagram_world), follow_redirects=True)
        self.llm = LLM(self.cfg.llm)

    def tearDown(self):
        self.state.close()
        self.llm_server.__exit__(None, None, None)
        self._tmp.cleanup()

    def fetch(self, slides: list[str]):
        with patch.object(LinkFetcher, "_ytdlp_info", return_value=None), \
                patch.object(LinkFetcher, "_instagram_slides", return_value=slides), \
                patch("digest.media.check_public_url"):
            return fetch_links(self.cfg, self.state, self.llm, http=self.http, store_images=False)

    def vision_requests(self) -> list[dict]:
        return [r for r in self.llm_server.requests if r["model"] == "fake-vision"]

    def test_image_only_post_is_described_slide_by_slide(self):
        docs, errors = self.fetch(SLIDES)

        self.assertEqual(errors, [])
        self.assertEqual(len(docs), 1)
        for number in (1, 2, 3):
            self.assertIn(f"[Immagine {number}]", docs[0].text)
        self.assertEqual(len(self.vision_requests()), 3)
        self.assertEqual(docs[0].meta["note"], "idee torta")

    def test_failed_carousel_walk_marks_the_link_failed_instead_of_inventing_content(self):
        docs, errors = self.fetch([])

        self.assertEqual(docs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.vision_requests(), [])
        status = self.state.db.execute("SELECT status FROM links WHERE url=?", (POST_URL,)).fetchone()[0]
        self.assertEqual(status, "error")


class CookiesFileTests(unittest.TestCase):
    def test_netscape_cookies_file_feeds_the_browser_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tabc123\n"
                ".example.com\tTRUE\t/\tFALSE\t4102444800\tother\tzzz\n",
                encoding="utf-8",
            )
            cfg = load_config(write_config(Path(tmp), "http://127.0.0.1:1/v1"))
            cfg.links.cookies_file = str(cookies)

            loaded = LinkFetcher(cfg, None)._browser_cookies()

        self.assertEqual([(c["name"], c["value"]) for c in loaded], [("sessionid", "abc123")])

    def test_missing_cookies_file_degrades_to_no_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_config(Path(tmp), "http://127.0.0.1:1/v1"))
            cfg.links.cookies_file = str(Path(tmp) / "nope.txt")

            self.assertEqual(LinkFetcher(cfg, None)._browser_cookies(), [])

    def test_cookies_file_wins_over_cookies_from_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tabc123\n",
                encoding="utf-8",
            )
            cfg = load_config(write_config(Path(tmp), "http://127.0.0.1:1/v1"))
            cfg.links.cookies_file = str(cookies)
            cfg.links.cookies_from_browser = "chrome"
            fetcher = LinkFetcher(cfg, None)

            browser_reader = MagicMock(side_effect=AssertionError("browser used"))
            fake_yt_dlp = {"yt_dlp": MagicMock(), "yt_dlp.cookies": MagicMock(extract_cookies_from_browser=browser_reader)}
            with patch.dict(sys.modules, fake_yt_dlp):
                loaded = fetcher._browser_cookies()

            self.assertEqual([(c["name"], c["value"]) for c in loaded], [("sessionid", "abc123")])
            cookiefile = fetcher._ytdlp_cookie_opts()["cookiefile"]
            self.assertNotEqual(cookiefile, str(cookies))  # yt-dlp rewrites its jar: it must never get the :ro original
            self.assertEqual(Path(cookiefile).read_text(encoding="utf-8"), cookies.read_text(encoding="utf-8"))

    def test_browser_cookies_are_the_fallback_when_no_file_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_config(Path(tmp), "http://127.0.0.1:1/v1"))
            cfg.links.cookies_from_browser = "firefox"

            self.assertEqual(LinkFetcher(cfg, None)._ytdlp_cookie_opts(), {"cookiesfrombrowser": ("firefox",)})


if __name__ == "__main__":
    unittest.main()
