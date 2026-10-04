# tests/e2e/test_vault_export.py
import tempfile
import unittest
from pathlib import Path

from digest.state import State

from .fakes import run_digest, write_config

ARTICLE = "https://blog.example.com/torta"
RECIPE = "https://blog.example.com/pasta"
PRIVATE = "https://blog.example.com/privato"
PENDING = "https://blog.example.com/da-fare"


def analysis(url: str, title: str, area: str) -> dict:
    return {"url": url, "platform": "web", "title": title, "summary": f"About {title}", "key_points": ["punto"],
            "tags": ["ricetta"], "actions": [], "worth_it": "alta", "area": area, "image": None}


class VaultExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = write_config(self.root, "http://127.0.0.1:1/v1")
        with self.config.open("a", encoding="utf-8") as fh:
            fh.write(f'\n[obsidian]\nenabled = false\nvault = "{self.vault}"\n')
        (self.root / "data").mkdir()
        with State(self.root / "data" / "state.sqlite") as state:
            state.save_area("riservato", "Riservato", "fuori dal vault", "#888888", exclude_from_vault=True)
            for url, title, area in ((ARTICLE, "Torta soffice", "altro"), (RECIPE, "Pasta fresca", "altro"),
                                     (PRIVATE, "Documento privato", "riservato")):
                state.add_link(url, "")
                state.link_done(url, title, analysis(url, title, area))
            state.add_link(PENDING, "")

    def tearDown(self):
        self._tmp.cleanup()

    def notes(self) -> dict[str, str]:
        links_dir = self.vault / "Digest" / "Link"
        return {p.stem: p.read_text(encoding="utf-8") for p in links_dir.glob("*.md")}

    def test_links_digested_before_the_vault_get_their_notes(self):
        result = run_digest(self.config, "vault-export")

        self.assertEqual(result.returncode, 0, result.stderr)
        notes = self.notes()
        self.assertEqual(sorted(notes), ["Pasta fresca", "Torta soffice"])
        self.assertIn(f'url: "{ARTICLE}"', notes["Torta soffice"])
        self.assertIn("About Torta soffice", notes["Torta soffice"])

    def test_links_in_areas_kept_out_of_the_vault_and_unprocessed_links_are_skipped(self):
        run_digest(self.config, "vault-export")

        self.assertNotIn("Documento privato", self.notes())
        self.assertNotIn("da-fare", " ".join(self.notes().values()))

    def test_second_run_writes_nothing_and_keeps_manual_edits(self):
        run_digest(self.config, "vault-export")
        note = self.vault / "Digest" / "Link" / "Torta soffice.md"
        edited = note.read_text(encoding="utf-8") + "\nLa mia nota personale\n"
        note.write_text(edited, encoding="utf-8")

        result = run_digest(self.config, "vault-export")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Note scritte: 0", result.stdout)
        self.assertEqual(note.read_text(encoding="utf-8"), edited)

    def test_missing_vault_is_reported_instead_of_crashing(self):
        (self.vault).rmdir()

        result = run_digest(self.config, "vault-export")

        self.assertEqual(result.returncode, 2)
        self.assertIn("il vault non esiste", result.stderr)


if __name__ == "__main__":
    unittest.main()
