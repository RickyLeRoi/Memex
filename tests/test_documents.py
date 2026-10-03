import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from digest import __main__ as cli
from digest.config import Config, ObsidianConfig
from digest.extract import summarize_link
from digest.obsidian import write_obsidian
from digest.purge import Purger
from digest.report import item_line, link_block, write_reports
from digest.sources.documents import DOC_SCHEME, PdfError, extract_pdf, pdf_doc, store_pdf
from digest.sources.links import fetch_links
from digest.state import State
from digest.tags import Vocabulary
from digest.web.server import create_server
from tests.pdfs import JPEG, build_pdf
from tests.test_web import make_service

LONG_TEXT = "Polizza assicurativa auto. Scadenza il 15 novembre 2026. Premio annuo 420 euro. Agenzia Rossi, Milano."
TEXT_PDF = build_pdf([{"text": LONG_TEXT}])
SCAN_PDF = build_pdf([{"jpeg": JPEG}, {"text": "short"}, {}])
TRANSCRIPTION = "Fattura n. 123 del 3 ottobre 2026, importo 420 euro, scadenza pagamento 15 novembre 2026."


class Vision:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def describe_image(self, data, mime, prompt, max_tokens=1024):
        self.calls.append((mime, max_tokens))
        return TRANSCRIPTION

    def chat_json(self, system, user):
        return {"title": "Polizza auto", "summary": "s", "tags": ["assicurazione"],
                "items": [{"kind": "deadline", "title": "Rinnovo polizza", "due": "2026-11-15", "ref": 1,
                           "tags": ["assicurazione"]}]}


def config(tmp: Path, **links) -> Config:
    cfg = Config(data_dir=str(tmp / "data"), reports_dir=str(tmp / "reports"), base_dir=tmp)
    cfg.llm.vision_model = "vision"
    for key, value in links.items():
        setattr(cfg.links, key, value)
    return cfg


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = config(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()


class StorePdfTests(TempCase):
    def test_valid_pdf_is_stored_once_with_a_content_name(self):
        first = store_pdf(self.tmp / "docs", TEXT_PDF, 100_000)
        self.assertEqual(first, store_pdf(self.tmp / "docs", TEXT_PDF, 100_000))
        self.assertRegex(first, r"^[0-9a-f]{40}\.pdf$")
        self.assertEqual([p.name for p in (self.tmp / "docs").iterdir()], [first])

    def test_refusals(self):
        for label, data in {"empty": b"", "html": b"<html>%PDF-</html>", "png": b"\x89PNG\r\n\x1a\n" + b"x" * 9,
                            "leading junk": b" %PDF-1.4"}.items():
            with self.subTest(label=label), self.assertRaises(PdfError):
                store_pdf(self.tmp / "docs", data, 100_000)
        with self.assertRaises(PdfError):
            store_pdf(self.tmp / "docs", TEXT_PDF, 100)
        self.assertFalse((self.tmp / "docs").exists())


class ExtractTests(TempCase):
    def test_text_pdf_needs_no_vision(self):
        vision = Vision()
        content = extract_pdf(TEXT_PDF, self.cfg, vision)
        self.assertIn(LONG_TEXT, content.text)
        self.assertEqual((content.pages, content.scanned_pages, vision.calls), (1, 0, []))
        self.assertIsNone(content.cover)

    def test_scanned_page_is_read_by_the_vision_model_per_page(self):
        vision = Vision()
        content = extract_pdf(SCAN_PDF, self.cfg, vision)
        self.assertIn(f"[Pagina 1, scansione]\n{TRANSCRIPTION}", content.text)
        self.assertIn("[Pagina 2]\nshort", content.text)
        self.assertEqual(vision.calls, [("image/jpeg", self.cfg.llm.max_tokens)])
        self.assertEqual((content.scanned_pages, content.cover), (1, JPEG))

    def test_page_without_text_or_extractable_image_is_reported_with_the_reason(self):
        content = extract_pdf(SCAN_PDF, self.cfg, Vision())
        self.assertIn("Pagine senza contenuto leggibile: 3", content.text)
        self.assertIn("Pillow o PyMuPDF", content.text)

    def test_scanned_pages_without_vision_model_give_an_actionable_error(self):
        self.cfg.llm.vision_model = ""
        with self.assertRaisesRegex(PdfError, "vision_model"):
            extract_pdf(SCAN_PDF, self.cfg, Vision())

    def test_dry_run_does_not_call_the_model(self):
        self.assertIn("dry-run", extract_pdf(SCAN_PDF, self.cfg, None).text)

    def test_page_cap_is_enforced_and_declared(self):
        pdf = build_pdf([{"text": f"{LONG_TEXT} Page {i}"} for i in range(1, 6)])
        self.cfg.links.max_pdf_pages = 2
        content = extract_pdf(pdf, self.cfg, None)
        self.assertEqual((content.pages, content.read_pages), (5, 2))
        self.assertIn("lette le prime 2 pagine su 5", content.text)
        self.assertNotIn("Page 3", content.text)

    def test_oversized_page_image_is_not_sent_to_the_model(self):
        self.cfg.links.max_screenshot_bytes = 10
        content_text = ""
        with self.assertRaises(PdfError):
            content_text = extract_pdf(build_pdf([{"jpeg": JPEG}]), self.cfg, Vision()).text
        self.assertEqual(content_text, "")

    def test_encrypted_pdf_is_refused_without_trying_to_guess(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(TEXT_PDF)))
        writer.encrypt("secret", algorithm="RC4-128")
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesRegex(PdfError, "password"):
            extract_pdf(buffer.getvalue(), self.cfg, None)

    def test_broken_and_empty_pdfs_fail_cleanly(self):
        with self.assertRaises(PdfError):
            extract_pdf(b"%PDF-1.4\nthis is not really a pdf", self.cfg, None)
        with self.assertRaises(PdfError):
            extract_pdf(build_pdf([{}, {}]), self.cfg, None)


class PdfDocTests(TempCase):
    def test_doc_header_title_and_cover(self):
        name = store_pdf(self.cfg.data_path / "docs", SCAN_PDF, 100_000)
        doc = pdf_doc(self.cfg, Vision(), f"{DOC_SCHEME}{name}", "polizza")
        self.assertIn("Tipo: documento PDF", doc.text)
        self.assertIn("Pagine: 3", doc.text)
        self.assertIn("Nota dell'utente: polizza", doc.text)
        self.assertEqual((doc.title, doc.meta["platform"]), ("polizza", "documento"))
        self.assertTrue((self.cfg.data_path / "media" / doc.meta["image"]).is_file())

    def test_bad_references(self):
        for url in (f"{DOC_SCHEME}../../x.pdf", f"{DOC_SCHEME}{'0' * 40}.pdf", f"{DOC_SCHEME}x"):
            with self.subTest(url=url), self.assertRaises(PdfError):
                pdf_doc(self.cfg, Vision(), url, "")

    def test_long_documents_are_truncated_for_the_model(self):
        self.cfg.links.max_chars = 50
        name = store_pdf(self.cfg.data_path / "docs", TEXT_PDF, 100_000)
        self.assertIn("[...troncato...]", pdf_doc(self.cfg, None, f"{DOC_SCHEME}{name}", "").text)


class WebPdfTests(TempCase):
    def fetch(self, body: bytes, content_type: str = "application/pdf"):
        def handler(request):
            return httpx.Response(200, content=body, headers={"content-type": content_type})

        with State(self.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://docs.example.com/policy.pdf")
            return fetch_links(self.cfg, state, Vision(), httpx.Client(transport=httpx.MockTransport(handler)))

    def test_pdf_link_goes_through_the_same_pipeline(self):
        docs, errors = self.fetch(TEXT_PDF)
        self.assertEqual(errors, [])
        self.assertIn(LONG_TEXT, docs[0].text)

    def test_scanned_pdf_link_is_read_by_the_vision_model(self):
        docs, _ = self.fetch(SCAN_PDF)
        self.assertIn(TRANSCRIPTION, docs[0].text)

    def test_oversized_pdf_link_fails_with_the_reason(self):
        self.cfg.links.max_pdf_bytes = 100
        docs, errors = self.fetch(TEXT_PDF)
        self.assertEqual(docs, [])
        self.assertIn("troppo grande", errors[0])


class QueueAndRoutesTests(TempCase):
    def setUp(self):
        super().setUp()
        self.service = make_service(self.tmp)
        self.service.cfg.llm.vision_model = "vision"
        self.service.cfg.links.max_pdf_bytes = len(TEXT_PDF) + 200
        self.server = create_server(self.service, port=0, dist_dir=self.tmp / "none")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method: str, path: str, body: bytes | None = None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_upload_queues_the_document_and_it_can_be_downloaded(self):
        resp, data = self.request("POST", "/api/uploads/documents", TEXT_PDF, **{"Content-Type": "application/pdf"})
        self.assertEqual(resp.status, 200)
        queued = json.loads(data)
        self.assertTrue(queued["added"])
        self.assertEqual(self.service.list_links()[0]["url"], queued["url"])
        resp, body = self.request("GET", f"/api/docs/{queued['name']}")
        self.assertEqual((resp.status, body), (200, TEXT_PDF))
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_upload_rejections(self):
        pdf = {"Content-Type": "application/pdf"}
        self.assertEqual(self.request("POST", "/api/uploads/documents", TEXT_PDF, **{"Content-Type": "image/png"})[0].status, 415)
        self.assertEqual(self.request("POST", "/api/uploads/documents", b"<html>not a pdf</html>", **pdf)[0].status, 400)
        self.assertEqual(self.request("POST", "/api/uploads/documents", TEXT_PDF + b"x" * 1000, **pdf)[0].status, 413)
        self.assertEqual(self.service.list_links(), [])

    def test_only_content_addressed_names_are_served(self):
        for path in ("/api/docs/../../config.toml", "/api/docs/x.pdf", f"/api/docs/{'0' * 40}.pdf", "/api/docs/"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)

    def test_same_document_twice_is_not_queued_twice(self):
        first = self.service.queue_document(TEXT_PDF, "a")
        self.assertFalse(self.service.queue_document(TEXT_PDF, "b")["added"])
        self.assertEqual(first["url"], self.service.list_links()[0]["url"])


class PipelineAndDeleteTests(TempCase):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.cfg.obsidian = ObsidianConfig(enabled=True, vault=str(self.vault))
        self.state = State(self.cfg.data_path / "state.sqlite")

    def tearDown(self):
        self.state.close()
        super().tearDown()

    def ingest(self, data: bytes, note: str = "polizza") -> tuple[str, dict]:
        name = store_pdf(self.cfg.data_path / "docs", data, 1_000_000)
        url = f"{DOC_SCHEME}{name}"
        self.state.add_link(url, note)
        vision = Vision()
        docs, errors = fetch_links(self.cfg, self.state, vision)
        self.assertEqual(errors, [])
        result = {"started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 1}, "items": [],
                  "links": [], "errors": [], "highlights": []}
        args = SimpleNamespace(dry_run=False, no_advance=False)
        cli._run_links(self.cfg, self.state, vision, args, result, summarize_link, Vocabulary())
        self.assertEqual(docs[0].url, url)
        self.state.save_items("run1", result["items"])
        write_reports(result, self.cfg.reports_path)
        write_obsidian(result, self.cfg)
        return url, result

    def test_documents_also_yield_deadline_items_tied_to_the_document(self):
        url, result = self.ingest(SCAN_PDF)
        self.assertEqual([(i["kind"], i["ref_url"], i["due"]) for i in result["items"]],
                         [("deadline", url, "2026-11-15")])
        self.assertEqual(result["items"][0]["tags"], ["assicurazione"])
        self.assertEqual(self.state.link_analysis(url)["tags"], ["assicurazione"])

    def test_reports_and_notes_never_link_to_the_internal_reference(self):
        url, result = self.ingest(SCAN_PDF)
        self.assertNotIn(f"]({url})", link_block(result["links"][0]))
        self.assertNotIn(f"]({url})", item_line(result["items"][0]))
        self.assertNotIn("file://", (self.cfg.reports_path / "latest.md").read_text(encoding="utf-8").replace(url, ""))

    def test_vault_note_embeds_the_pdf_and_the_cover(self):
        url, result = self.ingest(SCAN_PDF)
        name = url.removeprefix(DOC_SCHEME)
        note = (self.vault / "Digest" / "Link" / "Polizza auto.md").read_text(encoding="utf-8")
        self.assertIn(f"![[{name}]]", note)
        self.assertIn(f"![[{result['links'][0]['image']}]]", note)
        self.assertTrue((self.vault / "Digest" / "Link" / "media" / name).is_file())

    def test_copy_documents_can_be_switched_off(self):
        self.cfg.obsidian.copy_documents = False
        url, _ = self.ingest(TEXT_PDF)
        self.assertFalse((self.vault / "Digest" / "Link" / "media" / url.removeprefix(DOC_SCHEME)).exists())

    def test_deleting_a_document_removes_pdf_copy_note_cover_and_items(self):
        url, result = self.ingest(SCAN_PDF)
        other_url, _ = self.ingest(TEXT_PDF, "altro")
        name, cover = url.removeprefix(DOC_SCHEME), result["links"][0]["image"]
        purger = Purger(self.cfg, self.state)
        outcome = purger.execute("link", url, purger.plan("link", url).token)
        self.assertTrue(outcome["ok"], outcome)
        self.assertFalse((self.cfg.data_path / "docs" / name).exists())
        self.assertFalse((self.cfg.data_path / "media" / cover).exists())
        self.assertFalse((self.vault / "Digest" / "Link" / "media" / name).exists())
        self.assertFalse((self.vault / "Digest" / "Link" / "Polizza auto.md").exists())
        self.assertEqual(self.state.items_for_ref(url), [])
        self.assertIsNone(self.state.link_row(url))
        self.assertTrue((self.cfg.data_path / "docs" / other_url.removeprefix(DOC_SCHEME)).is_file())
        self.assertIsNotNone(self.state.link_row(other_url))

    def test_impact_lists_the_document_files(self):
        url, _ = self.ingest(SCAN_PDF)
        purger = Purger(self.cfg, self.state)
        info = purger.describe(purger.plan("link", url))
        paths = " ".join(f["path"] for f in info["files"])
        self.assertIn(f"data:docs/{url.removeprefix(DOC_SCHEME)}", paths)
        self.assertEqual(info["db"], {"links": 1, "items": 1})


if __name__ == "__main__":
    unittest.main()
