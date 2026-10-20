# tests/e2e/test_browser.py
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from digest.config import load_config
from digest.web.server import DIST_DIR, create_server
from digest.web.service import Service

from .fakes import REPO_ROOT, FakeLLM, FakeSite, write_config

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pip install -e '.[e2e]' && playwright install chromium
    sync_playwright = None

JOB_TIMEOUT_MS = 60_000


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@unittest.skipUnless((DIST_DIR / "index.html").is_file(), "frontend not built: npm run build in ./frontend")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._playwright = sync_playwright().start()
        try:
            cls.browser = cls._playwright.chromium.launch()
        except PlaywrightError as e:
            cls._playwright.stop()
            raise unittest.SkipTest(f"chromium not available: {e}") from e

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._playwright.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.llm = FakeLLM().__enter__()
        self.site = FakeSite().__enter__()
        config_path = write_config(root, f"{self.llm.url}/v1")
        self._env = patch.dict(os.environ, {"PYTHONPATH": str(REPO_ROOT)})
        self._env.start()
        self.server = create_server(Service(load_config(config_path), config_path), port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.page = self.browser.new_page()
        self.page.set_default_timeout(10_000)
        self.addCleanup(self.page.close)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._env.stop()
        self.site.__exit__(None, None, None)
        self.llm.__exit__(None, None, None)
        self._tmp.cleanup()

    def open_links(self):
        self.page.goto(f"{self.base}/#links")
        expect(self.page.get_by_role("heading", name="Link esterni")).to_be_visible()

    def ingest(self, *paths: str):
        self.page.locator("#links-input").fill("\n".join(f"{self.site.url}{p}  nota {p.strip('/')}" for p in paths))
        self.page.get_by_role("button", name=f"Ingerisci {len(paths)} link").click()
        expect(self.page.get_by_text("Completato")).to_be_visible(timeout=JOB_TIMEOUT_MS)

    def test_tabs_switch_through_the_url_hash(self):
        self.page.goto(self.base)
        expect(self.page.get_by_role("button", name="Dashboard")).to_have_attribute("aria-current", "page")

        self.page.get_by_role("button", name="Link").click()

        self.assertTrue(self.page.url.endswith("#links"))
        expect(self.page.get_by_role("heading", name="Link esterni")).to_be_visible()

    def test_invalid_url_is_flagged_and_cannot_be_submitted(self):
        self.open_links()
        self.page.locator("#links-input").fill("non-un-url")

        expect(self.page.get_by_text("URL non valido")).to_be_visible()
        expect(self.page.get_by_role("button", name="Ingerisci link", exact=True)).to_be_disabled()

    def test_ingest_shows_done_and_failed_links_in_the_history(self):
        self.open_links()
        self.ingest("/article", "/blocked")

        done_row = self.page.get_by_role("row").filter(has_text="Guida al refactoring")
        expect(done_row.locator(".badge")).to_have_text("ok")
        expect(done_row).to_contain_text("nota article")
        failed_row = self.page.get_by_role("row").filter(has_text="/blocked").first
        expect(failed_row.locator(".badge")).to_have_text("errore")
        expect(failed_row).to_contain_text("403")
        expect(self.page.get_by_role("button", name="Riprova i falliti")).to_be_visible()

    def test_details_show_the_model_analysis(self):
        self.open_links()
        self.ingest("/article")

        self.page.get_by_text("Dettagli").click()

        expect(self.page.get_by_text("Riassunto generato dal modello finto.")).to_be_visible()
        expect(self.page.get_by_text("punto uno")).to_be_visible()

    def test_recipe_details_render_ingredients_and_steps(self):
        self.open_links()
        self.ingest("/recipe")

        self.page.get_by_text("Dettagli").click()

        expect(self.page.get_by_text("200 g farina")).to_be_visible()
        expect(self.page.get_by_text("Sbatti le uova con lo zucchero.")).to_be_visible()

    def test_deleting_a_link_removes_it_from_the_history(self):
        self.open_links()
        self.ingest("/article")

        self.page.get_by_role("button", name="Elimina Guida al refactoring").click()
        dialog = self.page.get_by_role("dialog")
        expect(dialog).to_contain_text("non si può annullare")
        dialog.get_by_role("button", name="Elimina definitivamente").click()
        expect(dialog).to_contain_text("Fatto")
        dialog.get_by_role("button", name="Chiudi").click()

        expect(self.page.get_by_text("Nessun link ancora.")).to_be_visible()

    def test_cancelling_the_delete_dialog_keeps_the_link(self):
        self.open_links()
        self.ingest("/article")

        self.page.get_by_role("button", name="Elimina Guida al refactoring").click()
        self.page.get_by_role("dialog").get_by_role("button", name="Annulla").click()

        expect(self.page.get_by_role("dialog")).to_have_count(0)
        expect(self.page.get_by_role("row").filter(has_text="Guida al refactoring")).to_be_visible()

    def test_dashboard_counts_the_ingested_link(self):
        self.open_links()
        self.ingest("/article")

        self.page.get_by_role("button", name="Dashboard").click()

        expect(self.page.get_by_role("main")).to_contain_text("Guida al refactoring", timeout=10_000)


if __name__ == "__main__":
    unittest.main()
