import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from digest.config import Config, ObsidianConfig
from digest.extract import summarize_link
from digest.media import ImageRejected, store_upload
from digest.obsidian import write_obsidian
from digest.purge import Purger
from digest.report import link_block, write_reports
from digest.sources.links import IMAGE_SCHEME, fetch_links, screenshot_doc
from digest.state import State
from digest.web.server import create_server
from tests.test_web import make_service

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"w" * 32
TEXT = "Ingredienti: 200 g di farina, 2 uova. Preparazione: impastare e cuocere. Screenshot di una ricetta."


class Vision:
    def __init__(self, answer: str = TEXT):
        self.answer = answer
        self.calls: list[tuple[str, int]] = []

    def describe_image(self, data, mime, prompt, max_tokens=1024):
        self.calls.append((mime, max_tokens))
        return self.answer

    def chat_json(self, system, user):
        return {"title": "Ricetta dello screenshot", "summary": "x", "tags": ["ricette"]}


class StoreUploadTests(unittest.TestCase):
    def test_png_jpeg_webp_are_accepted_and_named_after_their_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data, ext in ((PNG, "png"), (JPEG, "jpg"), (WEBP, "webp")):
                self.assertTrue(store_upload(Path(tmp), data, 1000).endswith(f".{ext}"))

    def test_everything_else_is_refused(self):
        refused = {
            "empty": b"", "gif": b"GIF89a" + b"g" * 20, "svg": b"<svg onload=alert(1)></svg>",
            "html": b"<html>image/png</html>", "pdf": b"%PDF-1.7 fake", "oversize": PNG + b"x" * 2000,
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, data in refused.items():
                with self.subTest(label=label), self.assertRaises(ImageRejected):
                    store_upload(Path(tmp), data, 1000)
            self.assertFalse(list(Path(tmp).iterdir()))


class ScreenshotDocTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp)
        self.cfg.llm.vision_model = "vision"
        self.name = store_upload(self.cfg.data_path / "media", PNG, 1000)
        self.url = f"{IMAGE_SCHEME}{self.name}"

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_carries_the_transcription_the_note_and_the_image(self):
        vision = Vision()
        doc = screenshot_doc(self.cfg, vision, self.url, "ricetta di Anna")
        self.assertIn(TEXT, doc.text)
        self.assertIn("Nota dell'utente: ricetta di Anna", doc.text)
        self.assertEqual((doc.title, doc.meta["image"], doc.meta["platform"]), ("ricetta di Anna", self.name, "screenshot"))
        self.assertEqual(vision.calls, [("image/png", self.cfg.llm.max_tokens)])

    def test_missing_vision_model_gives_an_actionable_error(self):
        self.cfg.llm.vision_model = ""
        with self.assertRaisesRegex(ValueError, "vision_model"):
            screenshot_doc(self.cfg, Vision(), self.url, "")

    def test_nothing_read_is_an_error_not_an_empty_document(self):
        with self.assertRaisesRegex(ValueError, "non ha letto"):
            screenshot_doc(self.cfg, Vision("ok"), self.url, "")

    def test_dry_run_does_not_call_the_model(self):
        self.assertIn("dry-run", screenshot_doc(self.cfg, None, self.url, "").text)

    def test_bad_references_are_refused(self):
        for url in (f"{IMAGE_SCHEME}../../secret.txt", f"{IMAGE_SCHEME}{'0' * 40}.png", f"{IMAGE_SCHEME}x"):
            with self.subTest(url=url), self.assertRaises((ValueError, FileNotFoundError)):
                screenshot_doc(self.cfg, Vision(), url, "")


class QueueAndPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.service = make_service(self.tmp)
        self.cfg = self.service.cfg
        self.cfg.llm.vision_model = "vision"
        self.cfg.obsidian = ObsidianConfig(enabled=True, vault=str(self.vault))

    def tearDown(self):
        self._tmp.cleanup()

    def test_queue_screenshot_adds_a_pending_row_once(self):
        first = self.service.queue_screenshot(PNG, "  my note  ")
        again = self.service.queue_screenshot(PNG, "other")
        self.assertTrue(first["added"])
        self.assertFalse(again["added"])
        row = self.service.list_links()[0]
        self.assertEqual((row["url"], row["note"], row["status"]), (first["url"], "my note", "pending"))

    def test_uploading_a_failed_screenshot_again_retries_it(self):
        queued = self.service.queue_screenshot(PNG, "")
        with State(self.cfg.data_path / "state.sqlite") as state:
            state.link_failed(queued["url"], "boom")
        self.assertTrue(self.service.queue_screenshot(PNG, "")["added"])
        self.assertEqual(self.service.list_links()[0]["status"], "pending")

    def test_full_pipeline_then_delete_removes_everything(self):
        queued = self.service.queue_screenshot(PNG, "ricetta")
        vision = Vision()
        with State(self.cfg.data_path / "state.sqlite") as state:
            docs, errors = fetch_links(self.cfg, state, vision)
            self.assertEqual(errors, [])
            ln = summarize_link(vision, self.cfg, docs[0])
            ln["image"] = docs[0].meta["image"]
            state.link_done(docs[0].url, ln["title"], ln)
            result = {"started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 1}, "items": [],
                      "links": [ln], "errors": [], "highlights": []}
            write_reports(result, self.cfg.reports_path)
            write_obsidian(result, self.cfg)

            note = (self.vault / "Digest" / "Link" / "Ricetta dello screenshot.md").read_text(encoding="utf-8")
            self.assertIn(f"![[{queued['name']}]]", note)
            self.assertNotIn("](image://", note)
            self.assertNotIn("](image://", (self.cfg.reports_path / "latest.md").read_text(encoding="utf-8"))
            self.assertTrue((self.vault / "Digest" / "Link" / "media" / queued["name"]).is_file())

            purger = Purger(self.cfg, state)
            outcome = purger.execute("link", queued["url"], purger.plan("link", queued["url"]).token)
            self.assertTrue(outcome["ok"], outcome)
            self.assertFalse((self.cfg.data_path / "media" / queued["name"]).exists())
            self.assertFalse((self.vault / "Digest" / "Link" / "media" / queued["name"]).exists())
            self.assertFalse((self.vault / "Digest" / "Link" / "Ricetta dello screenshot.md").exists())
            self.assertIsNone(state.link_row(queued["url"]))

    def test_unreadable_screenshot_fails_the_row_with_the_reason(self):
        queued = self.service.queue_screenshot(PNG, "")
        self.cfg.llm.vision_model = ""
        with State(self.cfg.data_path / "state.sqlite") as state:
            docs, errors = fetch_links(self.cfg, state, Vision())
            self.assertEqual(docs, [])
            self.assertIn("vision_model", errors[0])
            self.assertEqual(state.link_row(queued["url"])["status"], "error")

    def test_report_block_does_not_link_to_the_internal_reference(self):
        block = link_block({"url": f"{IMAGE_SCHEME}abc.png", "title": "Shot", "summary": "s"})
        self.assertTrue(block.startswith("### Shot"))
        self.assertNotIn("image://", block)


class UploadRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self._tmp.name))
        self.service.cfg.links.max_screenshot_bytes = 500
        self.server = create_server(self.service, port=0, dist_dir=Path(self._tmp.name) / "none")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def post(self, body: bytes, content_type: str = "image/png", **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/uploads/images", body, {"Content-Type": content_type, **headers})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_upload_queues_the_screenshot_with_its_note(self):
        status, data = self.post(PNG, **{"X-Note": quote("ricetta à la carte")})
        self.assertEqual(status, 200)
        self.assertTrue(data["added"])
        row = self.service.list_links()[0]
        self.assertEqual((row["url"], row["note"], row["image"]), (data["url"], "ricetta à la carte", None))

    def test_rejections(self):
        self.assertEqual(self.post(PNG, content_type="application/json")[0], 415)
        self.assertEqual(self.post(b"<svg onload=alert(1)>", "image/png")[0], 400)
        self.assertEqual(self.post(b"")[0], 400)
        self.assertEqual(self.post(PNG + b"x" * 1000)[0], 413)
        self.assertEqual(self.service.list_links(), [])

    def test_foreign_host_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/uploads/images", PNG, {"Content-Type": "image/png", "Host": "evil.example.com"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()


if __name__ == "__main__":
    unittest.main()
