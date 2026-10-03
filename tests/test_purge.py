import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from digest.config import Config, ObsidianConfig
from digest.obsidian import write_obsidian
from digest.purge import Edit, NotFound, Purger, PurgeError, StaleImpact, Target, scrub_daily_note, url_pattern
from digest.report import write_reports
from digest.state import State

TARGET = "https://a.example.com/x"
SIBLING = "https://a.example.com/x2"  # shares a prefix with TARGET: must never be touched
OTHER = "https://b.example.org/other"


def link(url: str, title: str) -> dict:
    return {"url": url, "platform": "web", "note": "", "title": title, "summary": f"About {title}",
            "key_points": [f"{title} point"], "tags": ["t"], "actions": [], "worth_it": "media"}


def item(title: str, ref_url: str, source: str = "mail") -> dict:
    return {"kind": "task", "title": title, "details": "d", "owner": None, "due": None, "priority": "media",
            "source": source, "ref_title": "Doc", "ref_url": ref_url, "mine": False}


class PurgeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp,
                          obsidian=ObsidianConfig(enabled=True, vault=str(self.vault)))
        self.state = State(self.cfg.data_path / "state.sqlite")
        self.result = {
            "started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 3, "mail": 1},
            "items": [item("Reply to Anna", "https://mail/1"), item("Keep this one", "https://mail/2")],
            "links": [link(TARGET, "Target page"), link(SIBLING, "Sibling page"), link(OTHER, "Other page")],
            "errors": [f"link {TARGET}: boom"], "highlights": ["Read the target page", "Something else"],
        }
        for ln in self.result["links"]:
            self.state.add_link(ln["url"])
            self.state.link_done(ln["url"], ln["title"], ln)
        self.state.save_items("run1", self.result["items"])
        write_reports(self.result, self.cfg.reports_path)
        write_obsidian(self.result, self.cfg)
        (self.tmp / "links.txt").write_text(f"{TARGET}  my note\n{SIBLING}\n{OTHER}\n", encoding="utf-8")
        (self.cfg.data_path / "digest.log").write_text(
            f"INFO digest: == links ==\nERROR Errore su {TARGET}\nINFO fetched {SIBLING}\n", encoding="utf-8")
        dry = self.cfg.reports_path / "dryrun"
        dry.mkdir()
        (dry / "x_links.txt").write_text(
            f"[link] Target page\n{TARGET}\n\ntext\n\n==========\n\n[link] Sibling page\n{SIBLING}\n\nmore",
            encoding="utf-8")
        self.purger = Purger(self.cfg, self.state)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def delete(self, kind: str, ident: str) -> dict:
        plan = self.purger.plan(kind, ident)
        return self.purger.execute(kind, ident, plan.token)

    def all_text(self, root: Path) -> str:
        suffixes = (".md", ".json", ".txt", ".log")
        return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file() and p.suffix in suffixes)


class LinkDeletionTests(PurgeCase):
    def test_link_disappears_from_every_location(self):
        result = self.delete("link", TARGET)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.state.link_row(TARGET))
        self.assertFalse(url_pattern(TARGET).search((self.tmp / "links.txt").read_text(encoding="utf-8")))
        self.assertFalse(url_pattern(TARGET).search(self.all_text(self.cfg.reports_path)))
        self.assertFalse(url_pattern(TARGET).search((self.cfg.data_path / "digest.log").read_text(encoding="utf-8")))
        self.assertFalse(url_pattern(TARGET).search(self.all_text(self.vault)))
        self.assertFalse((self.vault / "Digest" / "Link" / "Target page.md").exists())

    def test_sibling_url_sharing_a_prefix_is_untouched(self):
        self.delete("link", TARGET)
        self.assertIsNotNone(self.state.link_row(SIBLING))
        self.assertIn(SIBLING, (self.tmp / "links.txt").read_text(encoding="utf-8"))
        self.assertIn(SIBLING, (self.cfg.data_path / "digest.log").read_text(encoding="utf-8"))
        self.assertTrue((self.vault / "Digest" / "Link" / "Sibling page.md").exists())
        self.assertIn("Sibling page", self.all_text(self.vault))
        self.assertIn(SIBLING, (self.cfg.reports_path / "dryrun" / "x_links.txt").read_text(encoding="utf-8"))

    def test_report_json_keeps_other_entries_and_drops_highlights(self):
        self.delete("link", TARGET)
        data = json.loads(next(self.cfg.reports_path.glob("digest_*.json")).read_text(encoding="utf-8"))
        self.assertEqual({ln["url"] for ln in data["links"]}, {SIBLING, OTHER})
        self.assertEqual(data["highlights"], [])
        self.assertEqual(data["stats"]["link"], 2)
        self.assertEqual(data["errors"], [])
        latest = (self.cfg.reports_path / "latest.md").read_text(encoding="utf-8")
        self.assertIn("Other page", latest)
        self.assertNotIn("Target page", latest)

    def test_link_does_not_come_back_on_next_sync(self):
        from digest.sources.links import sync_links

        self.delete("link", TARGET)
        sync_links(self.cfg, self.state)
        self.assertIsNone(self.state.link_row(TARGET))

    def test_delete_is_idempotent(self):
        self.delete("link", TARGET)
        self.assertTrue(self.delete("link", TARGET)["ok"])

    def test_dryrun_file_removed_when_all_blocks_go(self):
        self.delete("link", SIBLING)
        self.delete("link", TARGET)
        self.assertFalse((self.cfg.reports_path / "dryrun" / "x_links.txt").exists())


class EmptiedReportTests(PurgeCase):
    ONLY = "https://c.example.net/only"

    def add_report_with_only_one_link(self) -> None:
        extra = {"started": datetime(2026, 10, 2, 10, 0).astimezone(), "stats": {"link": 1}, "items": [],
                 "links": [link(self.ONLY, "Only page")], "errors": [], "highlights": ["Only highlight"]}
        self.state.add_link(self.ONLY)
        self.state.link_done(self.ONLY, "Only page", extra["links"][0])
        write_reports(extra, self.cfg.reports_path)

    def test_report_left_empty_is_removed_not_kept_as_a_shell(self):
        self.add_report_with_only_one_link()
        self.assertTrue(self.delete("link", self.ONLY)["ok"])
        names = {p.name for p in self.cfg.reports_path.glob("digest_*")}
        self.assertNotIn("digest_2026-10-02_1000.json", names)
        self.assertNotIn("digest_2026-10-02_1000.md", names)
        self.assertIn("digest_2026-10-02_0930.json", names)

    def test_latest_follows_the_newest_surviving_report(self):
        self.add_report_with_only_one_link()
        self.delete("link", self.ONLY)
        latest = (self.cfg.reports_path / "latest.md").read_text(encoding="utf-8")
        self.assertNotIn("Only page", latest)
        self.assertIn("Other page", latest)

    def test_latest_is_removed_when_no_report_survives(self):
        for path in self.cfg.reports_path.glob("digest_*"):
            path.unlink()
        self.add_report_with_only_one_link()
        self.delete("link", self.ONLY)
        self.assertEqual(list(self.cfg.reports_path.glob("digest_*")), [])
        self.assertFalse((self.cfg.reports_path / "latest.md").exists())

    def test_report_that_was_already_empty_is_not_touched(self):
        empty = {"started": datetime(2026, 10, 2, 8, 0).astimezone(), "stats": {}, "items": [], "links": [],
                 "errors": [], "highlights": []}
        write_reports(empty, self.cfg.reports_path)
        (self.cfg.reports_path / "latest.md").write_text("restore", encoding="utf-8")
        write_reports(self.result, self.cfg.reports_path)
        self.delete("link", TARGET)
        self.assertTrue((self.cfg.reports_path / "digest_2026-10-02_0800.json").exists())


class ItemAndDocDeletionTests(PurgeCase):
    def test_doc_deletion_removes_items_and_report_entries(self):
        result = self.delete("doc", "https://mail/1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.state.items_for_ref("https://mail/1"), [])
        self.assertEqual(len(self.state.items_for_ref("https://mail/2")), 1)
        text = self.all_text(self.cfg.reports_path) + self.all_text(self.vault)
        self.assertNotIn("Reply to Anna", text)
        self.assertIn("Keep this one", text)

    def test_single_item_deletion(self):
        item_id = self.state.items_for_ref("https://mail/2")[0]["id"]
        self.delete("item", str(item_id))
        self.assertIsNone(self.state.item_by_id(item_id))
        self.assertNotIn("Keep this one", self.all_text(self.vault))
        self.assertIn("Reply to Anna", self.all_text(self.vault))

    def test_unknown_item_raises(self):
        with self.assertRaises(NotFound):
            self.purger.plan("item", "9999")

    def test_unknown_kind_raises(self):
        with self.assertRaises(PurgeError):
            self.purger.plan("everything", "x")


class SafetyTests(PurgeCase):
    def test_stale_token_is_refused_and_nothing_changes(self):
        with self.assertRaises(StaleImpact):
            self.purger.execute("link", TARGET, "not-the-token")
        self.assertIsNotNone(self.state.link_row(TARGET))
        self.assertIn(TARGET, (self.tmp / "links.txt").read_text(encoding="utf-8"))

    def test_a_transient_file_lock_is_retried(self):
        from unittest import mock

        from digest import purge

        real, calls = purge._write_atomic, {"n": 0}

        def flaky(path, text):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("locked by the antivirus")
            real(path, text)

        with mock.patch("digest.purge._write_atomic", side_effect=flaky):
            result = self.delete("link", TARGET)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.state.link_row(TARGET))

    def test_if_links_txt_cannot_be_cleaned_nothing_is_deleted(self):
        from unittest import mock

        with mock.patch("digest.purge._write_atomic", side_effect=PermissionError("locked")):
            result = self.delete("link", TARGET)
        self.assertFalse(result["ok"])
        self.assertIn("nothing was deleted", result["errors"][0])
        self.assertIsNotNone(self.state.link_row(TARGET))  # no half-deletion: the link could not come back from links.txt
        self.assertIn(TARGET, (self.tmp / "links.txt").read_text(encoding="utf-8"))
        self.assertTrue(self.delete("link", TARGET)["ok"])  # and trying again works once the lock is gone

    def test_plan_changes_nothing(self):
        before = (self.tmp / "links.txt").read_text(encoding="utf-8")
        plan = self.purger.plan("link", TARGET)
        self.assertGreater(len(plan.edits), 3)
        self.assertEqual((self.tmp / "links.txt").read_text(encoding="utf-8"), before)
        self.assertIsNotNone(self.state.link_row(TARGET))

    def test_path_outside_allowed_roots_is_refused(self):
        with tempfile.TemporaryDirectory() as outside:
            victim = Path(outside) / "victim.txt"
            victim.write_text("keep me", encoding="utf-8")
            with self.assertRaises(PurgeError):
                self.purger._apply(Edit(victim, None, "evil"))
            self.assertTrue(victim.exists())

    def test_foreign_markdown_note_in_links_folder_is_not_deleted(self):
        foreign = self.vault / "Digest" / "Link" / "My own note.md"
        foreign.write_text(f"My own thoughts about {TARGET}\n", encoding="utf-8")
        self.delete("link", TARGET)
        self.assertTrue(foreign.exists())

    def test_description_lists_files_without_touching_them(self):
        info = self.purger.describe(self.purger.plan("link", TARGET))
        self.assertEqual(info["db"], {"links": 1, "items": 0})
        paths = " ".join(f["path"] for f in info["files"])
        self.assertIn("links.txt", paths)
        self.assertIn("vault:Digest/Link/Target page.md", paths)


class DailyNoteTests(unittest.TestCase):
    NOTE = "\n".join([
        "---", "data: 2026-10-02", "---", "", "# Digest 2026-10-02", "",
        "## Run delle 09:30", "", "### In evidenza", "", "- Read the target", "",
        "### Link salvati", "", "- [[Target page]] — about it", "",
        "## Run delle 14:00", "", "### In evidenza", "", "- Untouched highlight", "",
        "### Link salvati", "", "- [[Other page]] — other", "",
    ])

    def test_affected_run_loses_highlights_and_empty_sections_unaffected_run_kept(self):
        target = Target("link", TARGET, "t", urls={TARGET}, stems={"Target page"})
        out = scrub_daily_note(self.NOTE, target)
        self.assertNotIn("Target page", out)
        self.assertNotIn("Read the target", out)
        self.assertNotIn("## Run delle 09:30", out)
        self.assertIn("Untouched highlight", out)
        self.assertIn("[[Other page]]", out)


if __name__ == "__main__":
    unittest.main()
