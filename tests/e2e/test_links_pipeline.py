# tests/e2e/test_links_pipeline.py
import tempfile
import unittest
from pathlib import Path

from digest.state import State

from .fakes import FakeLLM, FakeSite, run_digest, write_config


class LinksPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.llm = FakeLLM().__enter__()
        self.site = FakeSite().__enter__()
        self.config = write_config(self.root, f"{self.llm.url}/v1")

    def tearDown(self):
        self.site.__exit__(None, None, None)
        self.llm.__exit__(None, None, None)
        self._tmp.cleanup()

    def queue(self, *paths: str) -> None:
        lines = [f"{self.site.url}{p}  nota {p.strip('/')}" for p in paths]
        (self.root / "links.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def row(self, path: str) -> dict:
        with State(self.root / "data" / "state.sqlite") as state:
            found = state.db.execute(
                "SELECT status, attempts, COALESCE(error, '') FROM links WHERE url=?", (f"{self.site.url}{path}",)
            ).fetchone()
        self.assertIsNotNone(found, f"{path} not in the queue")
        return dict(zip(("status", "attempts", "error"), found))

    def analysis(self, path: str) -> dict:
        with State(self.root / "data" / "state.sqlite") as state:
            return state.link_analysis(f"{self.site.url}{path}")

    def test_article_goes_from_links_file_to_report(self):
        self.queue("/article")
        result = run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.row("/article")["status"], "done")
        analysis = self.analysis("/article")
        self.assertEqual(analysis["title"], "Guida al refactoring")
        self.assertEqual(analysis["summary"], "Riassunto generato dal modello finto.")
        self.assertEqual(analysis["note"], "nota article")
        reports = list((self.root / "reports").rglob("*"))
        self.assertTrue(any(p.is_file() and "Guida al refactoring" in p.read_text("utf-8") for p in reports))

    def test_recipe_page_is_stored_as_recipe(self):
        self.queue("/recipe")
        result = run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(result.returncode, 0, result.stderr)
        analysis = self.analysis("/recipe")
        self.assertEqual(analysis["kind"], "recipe")
        self.assertEqual(analysis["recipe"]["title"], "Torta soffice")
        self.assertEqual(len(analysis["recipe"]["steps"]), 2)
        self.assertGreaterEqual(len(analysis["recipe"]["ingredients"]), 3)

    def test_bot_wall_marks_the_link_failed_without_breaking_the_run(self):
        self.queue("/blocked", "/article")
        result = run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(result.returncode, 0, result.stderr)
        blocked = self.row("/blocked")
        self.assertEqual(blocked["status"], "error")
        self.assertIn("403", blocked["error"] or "")
        self.assertEqual(self.row("/article")["status"], "done")

    def test_page_without_content_fails_with_an_explanation(self):
        self.queue("/empty")
        run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(self.row("/empty")["status"], "error")

    def links_file_urls(self) -> list[str]:
        text = (self.root / "links.txt").read_text(encoding="utf-8")
        return [line.split()[0] for line in text.splitlines() if line.strip() and not line.startswith("#")]

    def test_ingested_links_leave_links_file_and_failed_ones_stay(self):
        self.queue("/article", "/blocked")
        with (self.root / "links.txt").open("a", encoding="utf-8") as fh:
            fh.write("# promemoria personale\n")

        run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(self.links_file_urls(), [f"{self.site.url}/blocked"])
        self.assertIn("# promemoria personale", (self.root / "links.txt").read_text(encoding="utf-8"))

    def test_links_file_is_untouched_when_the_run_does_not_advance(self):
        self.queue("/article")

        run_digest(self.config, "run", "--sources", "links", "--no-advance")
        run_digest(self.config, "run", "--sources", "links", "--dry-run")

        self.assertEqual(self.links_file_urls(), [f"{self.site.url}/article"])

    def test_only_local_processes_uploaded_files_and_leaves_the_web_queue_alone(self):
        self.queue("/article")
        screenshot = "image://" + "a" * 40 + ".png"
        (self.root / "data").mkdir(exist_ok=True)
        with State(self.root / "data" / "state.sqlite") as state:
            state.add_link(screenshot, "")
            state.add_link(f"{self.site.url}/recipe", "")

        result = run_digest(self.config, "run", "--sources", "links", "--only-local")

        self.assertEqual(result.returncode, 0, result.stderr)
        with State(self.root / "data" / "state.sqlite") as state:
            attempted = state.db.execute("SELECT status FROM links WHERE url=?", (screenshot,)).fetchone()[0]
            queued = state.db.execute("SELECT status, attempts FROM links WHERE url=?",
                                      (f"{self.site.url}/recipe",)).fetchone()
            imported = state.link_row(f"{self.site.url}/article")
        self.assertEqual(attempted, "error")  # the stored file does not exist, but it was the one picked up
        self.assertEqual(queued, ("pending", 0))
        self.assertIsNone(imported)  # links.txt was not imported
        self.assertEqual(self.site.hits, [])
        self.assertEqual(self.links_file_urls(), [f"{self.site.url}/article"])

    def test_second_run_does_not_reprocess_done_links(self):
        self.queue("/article")
        run_digest(self.config, "run", "--sources", "links")
        hits_after_first_run = len(self.site.hits)
        llm_calls_after_first_run = len(self.llm.requests)

        run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(len(self.site.hits), hits_after_first_run)
        self.assertEqual(len(self.llm.requests), llm_calls_after_first_run)

    def test_failed_link_is_retried_until_max_attempts(self):
        self.queue("/blocked")
        for _ in range(5):
            run_digest(self.config, "run", "--sources", "links")

        self.assertEqual(self.row("/blocked")["attempts"], 3)

    def test_retry_command_puts_failed_links_back_in_the_queue(self):
        self.queue("/blocked")
        run_digest(self.config, "run", "--sources", "links")
        self.assertEqual(self.row("/blocked")["status"], "error")

        run_digest(self.config, "links", "--retry")

        self.assertEqual(self.row("/blocked")["status"], "pending")

    def test_cli_add_queues_a_link_and_links_lists_it(self):
        run_digest(self.config, "add", f"{self.site.url}/article", "da leggere")
        listing = run_digest(self.config, "links")

        self.assertIn(f"{self.site.url}/article", listing.stdout)
        self.assertIn("pending", listing.stdout)

    def test_unreachable_llm_leaves_the_link_retryable(self):
        self.queue("/article")
        dead_config = write_config(self.root, "http://127.0.0.1:1/v1")
        run_digest(dead_config, "run", "--sources", "links")

        self.assertEqual(self.row("/article")["status"], "error")
        self.assertLess(self.row("/article")["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
