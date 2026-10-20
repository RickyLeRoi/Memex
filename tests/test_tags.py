import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from digest.config import Config
from digest.extract import extract_items, summarize_link
from digest.models import Doc
from digest.state import State
from digest.tags import Vocabulary, canonical, tag_rules
from tests.test_web import make_service

SOURCES = ("mail", "teams", "slack", "slack_tickets", "gmail", "jira", "notion")


class FakeLLM:
    def __init__(self, answer: dict):
        self.answer = answer
        self.systems: list[str] = []

    def chat_json(self, system: str, user: str) -> dict:
        self.systems.append(system)
        return self.answer


def doc(source: str) -> Doc:
    return Doc(source=source, id=f"{source}-1", title=f"{source} doc", text="some content", url=f"https://x/{source}")


class CanonicalTests(unittest.TestCase):
    def test_case_accents_and_separators(self):
        self.assertEqual(canonical("#Ricette Italiane"), "ricette-italiane")
        self.assertEqual(canonical("Città"), "citta")
        self.assertEqual(canonical("OnFeather  Free"), "onfeather-free")
        self.assertEqual(canonical("!!!"), "")
        self.assertEqual(canonical(None), "")


class ResolveTests(unittest.TestCase):
    def test_singular_plural_and_case_map_to_the_existing_tag(self):
        vocab = Vocabulary({"ricette": 3, "pomodoro": 2, "tools": 1, "onfeather-free": 4})
        self.assertEqual(vocab.resolve("ricetta"), "ricette")
        self.assertEqual(vocab.resolve("Ricette"), "ricette")
        self.assertEqual(vocab.resolve("pomodori"), "pomodoro")
        self.assertEqual(vocab.resolve("tool"), "tools")
        self.assertEqual(vocab.resolve("OnFeather Free"), "onfeather-free")

    def test_different_words_are_not_merged(self):
        vocab = Vocabulary({"caso": 5, "ai": 3, "date": 2})
        self.assertEqual(vocab.resolve("casa"), "casa")
        self.assertEqual(vocab.resolve("ais"), "ais")
        self.assertEqual(vocab.resolve("data"), "data")

    def test_unknown_tag_is_kept_as_new(self):
        self.assertEqual(Vocabulary({"ricette": 1}).resolve("Rust Lang"), "rust-lang")

    def test_multiword_tag_only_varies_the_last_word(self):
        vocab = Vocabulary({"pasta-fresche": 2})
        self.assertEqual(vocab.resolve("pasta fresca"), "pasta-fresche")


class PromptBlockTests(unittest.TestCase):
    def test_curated_first_then_most_used(self):
        vocab = Vocabulary({"alpha": 1, "beta": 9, "gamma": 5}, curated=["gamma-curated"])
        self.assertEqual(vocab.ordered(), ["gamma-curated", "beta", "gamma", "alpha"])

    def test_block_respects_the_caps(self):
        vocab = Vocabulary({f"tag-number-{i:04d}": i for i in range(600)})
        block = vocab.prompt_block()
        self.assertLessEqual(len(block), 1500)
        self.assertLessEqual(len(block.split(", ")), 150)
        self.assertTrue(block.startswith("tag-number-0599"))

    def test_rules_mention_the_vocabulary_and_reuse_only_when_not_empty(self):
        self.assertNotIn("già in uso", tag_rules(Vocabulary()))
        rules = tag_rules(Vocabulary({"onfeather-free": 2}))
        self.assertIn("onfeather-free", rules)
        self.assertIn("riusa", rules)


class FinalizeTests(unittest.TestCase):
    def test_model_tags_are_cleaned_deduped_and_capped(self):
        tags, origin = Vocabulary().finalize(["#A b", "a-b", "x", "y", "z", "w", "v", "u"], source="mail")
        self.assertEqual(tags[:2], ["a-b", "x"])
        self.assertEqual(len(tags), 6)
        self.assertEqual(set(origin.values()), {"model"})

    def test_accepts_a_comma_separated_string(self):
        self.assertEqual(Vocabulary().finalize("rust, docker", source="mail")[0], ["rust", "docker"])

    def test_empty_or_garbage_falls_back_to_auto_tags(self):
        for raw in (None, [], ["", "###"], ["ingredienti"], 42, {"a": 1}):
            tags, origin = Vocabulary().finalize(raw, source="jira", kind="task")
            self.assertEqual(tags, ["jira", "task"], raw)
            self.assertEqual(set(origin.values()), {"auto"})

    def test_auto_tags_never_enter_the_vocabulary_but_model_tags_do(self):
        vocab = Vocabulary()
        vocab.finalize(None, source="mail", kind="task")
        self.assertEqual(vocab.counts, {})
        vocab.finalize(["rust"], source="mail")
        self.assertEqual(vocab.counts, {"rust": 1})


class ExtractionTests(unittest.TestCase):
    def test_every_source_ends_up_with_at_least_one_tag(self):
        for source in SOURCES:
            for answer_tags in ([], None, ["", "ingredienti"], "garbage"):
                llm = FakeLLM({"items": [{"kind": "task", "title": "Do it", "ref": 1, "tags": answer_tags}]})
                items, _, errors = extract_items(llm, Config(), [doc(source)])
                self.assertEqual(errors, [])
                self.assertTrue(items[0]["tags"], (source, answer_tags))
                self.assertEqual(set(items[0]["tags"]) & {"ingredienti"}, set())

    def test_prompt_contains_the_vocabulary_and_a_similar_tag_is_reused(self):
        vocab = Vocabulary({"ricette": 3, "onfeather-free": 2})
        llm = FakeLLM({"items": [{"kind": "info", "title": "A recipe", "ref": 1, "tags": ["ricetta", "OnFeather Free"]}]})
        items, _, _ = extract_items(llm, Config(), [doc("mail")], vocab)
        self.assertIn("ricette, onfeather-free", llm.systems[0])
        self.assertEqual(items[0]["tags"], ["ricette", "onfeather-free"])

    def test_tags_created_in_one_run_are_reused_by_the_next_items(self):
        vocab = Vocabulary()
        llm = FakeLLM({"items": [{"kind": "info", "title": "One", "ref": 1, "tags": ["onfeather-free"]}]})
        extract_items(llm, Config(), [doc("mail")], vocab)
        llm.answer = {"items": [{"kind": "info", "title": "Two", "ref": 1, "tags": ["OnFeather Free"]}]}
        items, _, _ = extract_items(llm, Config(), [doc("jira")], vocab)
        self.assertIn("onfeather-free", llm.systems[-1])
        self.assertEqual(items[0]["tags"], ["onfeather-free"])

    def test_link_summary_always_has_tags(self):
        link = Doc(source="link", id="u", title="T", text="x", url="https://u", meta={"platform": "web"})
        tagged = summarize_link(FakeLLM({"title": "T", "tags": ["Ricetta"]}), Config(), link, Vocabulary({"ricette": 1}))
        self.assertEqual(tagged["tags"], ["ricette"])
        untagged = summarize_link(FakeLLM({"title": "T", "tags": []}), Config(), link)
        self.assertEqual(untagged["tags"], ["link", "web"])
        self.assertEqual(set(untagged["tag_origin"].values()), {"auto"})


class StateTagTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def test_items_saved_without_tags_get_auto_tags(self):
        with State(self.path) as state:
            state.save_items("r", [{"source": "mail", "kind": "task", "title": "t", "ref_url": "u"}])
            tags, origin = state.item_tags(1)
        self.assertEqual(tags, ["mail", "task"])
        self.assertEqual(set(origin.values()), {"auto"})

    def test_migration_backfills_items_created_before_tags_existed(self):
        db = sqlite3.connect(self.path)
        db.executescript(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT, kind TEXT, title TEXT,"
            " details TEXT, owner TEXT, due TEXT, priority TEXT, ref_title TEXT, ref_url TEXT, raw TEXT);"
            "INSERT INTO items(source, kind, title) VALUES ('teams', 'decision', 'old row');")
        db.commit()
        db.close()
        with State(self.path) as state:
            self.assertEqual(state.item_tags(1)[0], ["teams", "decision"])

    def test_migration_gives_auto_tags_to_links_digested_before_analyses_were_stored(self):
        with State(self.path) as state:
            state.add_link("https://www.github.com/x/y")
            state.db.execute("UPDATE links SET status='done', title='Y' WHERE url='https://www.github.com/x/y'")
            state.db.commit()
        with State(self.path) as state:
            analysis = state.link_analysis("https://www.github.com/x/y")
        self.assertEqual(analysis["tags"], ["link", "github-com"])
        self.assertEqual(set(analysis["tag_origin"].values()), {"auto"})

    def test_tag_counts_exclude_auto_tags(self):
        with State(self.path) as state:
            state.save_items("r", [{"source": "mail", "kind": "task", "title": "t", "tags": ["rust"],
                                    "tag_origin": {"rust": "model"}},
                                   {"source": "mail", "kind": "task", "title": "u"}])
            self.assertEqual(state.tag_counts(), {"rust": 1})

    def test_manual_tags_survive_a_reprocess_of_the_link(self):
        with State(self.path) as state:
            state.add_link("https://u")
            state.link_done("https://u", "T", {"tags": ["rust"], "tag_origin": {"rust": "model"}})
            analysis = state.link_analysis("https://u")
            analysis["tags"].append("mine")
            analysis["tag_origin"]["mine"] = "manual"
            state.set_link_analysis("https://u", analysis)
            state.link_done("https://u", "T", {"tags": ["docker"], "tag_origin": {"docker": "model"}})
            self.assertEqual(state.link_analysis("https://u")["tags"], ["docker", "mine"])

    def test_link_without_any_tag_gets_auto_tags(self):
        with State(self.path) as state:
            state.add_link("https://u")
            state.link_done("https://u", "T", {"platform": "web", "tags": []})
            self.assertEqual(state.link_analysis("https://u")["tags"], ["link", "web"])


class ServiceTagTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self._tmp.name))
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.save_items("r", [
                {"source": "mail", "kind": "task", "title": "One", "ref_url": "https://m/1", "ref_title": "Doc1",
                 "tags": ["onfeather-free", "rust"], "tag_origin": {"onfeather-free": "model", "rust": "model"}},
                {"source": "jira", "kind": "info", "title": "Two", "ref_url": "https://j/2", "ref_title": "Doc2",
                 "tags": ["onfeather-free"], "tag_origin": {"onfeather-free": "model"}},
                {"source": "mail", "kind": "info", "title": "Three", "ref_url": "https://m/3"},
            ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_curated_tag_links_items_across_documents_but_not_an_item_to_its_own_doc(self):
        self.service.set_link_tags(["onfeather-free"])
        graph = self.service.graph()
        tag_edges = [e for e in graph["edges"] if e["type"] == "tag"]
        pairs = {frozenset((e["source"], e["target"])) for e in tag_edges}
        self.assertIn(frozenset(("item:1", "item:2")), pairs)
        self.assertIn(frozenset(("https://m/1", "https://j/2")), pairs)
        self.assertNotIn(frozenset(("item:1", "https://m/1")), pairs)

    def test_uncurated_tags_never_link(self):
        self.assertEqual([e for e in self.service.graph()["edges"] if e["type"] == "tag"], [])

    def test_doc_node_inherits_the_union_of_its_items_tags(self):
        node = next(n for n in self.service.graph()["nodes"] if n["id"] == "https://m/1")
        self.assertEqual(set(node["tags"]), {"onfeather-free", "rust"})

    def test_every_node_has_tags(self):
        self.assertTrue(all(n["tags"] for n in self.service.graph()["nodes"]))

    def test_manual_tag_replaces_auto_placeholders_and_is_resolved_against_the_vocabulary(self):
        result = self.service.edit_tags("item", "3", ["Rust"], [])
        self.assertEqual(result["tags"], ["rust"])
        self.assertEqual(result["tag_origin"], {"rust": "manual"})

    def test_last_tag_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            self.service.edit_tags("item", "2", [], ["onfeather-free"])

    def test_doc_edit_applies_to_all_its_items(self):
        self.service.edit_tags("doc", "https://m/1", ["urgente"], ["rust"])
        node = next(n for n in self.service.graph()["nodes"] if n["id"] == "item:1")
        self.assertEqual(set(node["tags"]), {"onfeather-free", "urgente"})

    def test_available_tags_count_items_and_exclude_auto(self):
        available = {t["tag"]: t["count"] for t in self.service.link_tags()["available"]}
        self.assertEqual(available, {"onfeather-free": 2, "rust": 1})

    def test_link_tags_are_editable(self):
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://l")
            state.link_done("https://l", "L", {"tags": ["ricette"], "tag_origin": {"ricette": "model"}})
        result = self.service.edit_tags("link", "https://l", ["Ricetta", "cucina-italiana"], [])
        self.assertEqual(result["tags"], ["ricette", "cucina-italiana"])
        self.assertEqual(result["tag_origin"]["cucina-italiana"], "manual")


if __name__ == "__main__":
    unittest.main()
