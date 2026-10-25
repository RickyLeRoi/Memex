import json
import tempfile
import unittest
from pathlib import Path

import httpx

from digest.config import LLMConfig
from digest.llm import LLM, FallbackLLM, LinkLLM, LLMError
from digest.state import State


def llm_with(handler, **cfg) -> LLM:
    config = LLMConfig(base_url="http://x/v1", model="m", **cfg)
    return LLM(config, http=httpx.Client(transport=httpx.MockTransport(handler)))


def answering(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return handler


def failing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="no capacity")


class FallbackTests(unittest.TestCase):
    def test_the_backup_answers_when_the_primary_errors(self):
        llm = FallbackLLM(llm_with(failing), llm_with(answering("locale")))
        self.assertEqual(llm.chat([{"role": "user", "content": "ciao"}]), "locale")

    def test_the_primary_answer_is_used_when_it_works(self):
        llm = FallbackLLM(llm_with(answering("condiviso")), llm_with(answering("locale")))
        self.assertEqual(llm.chat([{"role": "user", "content": "ciao"}]), "condiviso")

    def test_a_connection_error_also_falls_back(self):
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        llm = FallbackLLM(llm_with(refused), llm_with(answering('{"ok": true}')))
        self.assertEqual(llm.chat_json("s", "u"), {"ok": True})

    def test_both_failing_surfaces_the_backup_error(self):
        llm = FallbackLLM(llm_with(failing), llm_with(failing))
        with self.assertRaises(LLMError):
            llm.chat([{"role": "user", "content": "ciao"}])


class LinkLLMTests(unittest.TestCase):
    def test_a_private_link_never_touches_the_shared_endpoint(self):
        local, shared = llm_with(answering("locale")), llm_with(answering("condiviso"))
        self.assertIs(LinkLLM(local, shared).for_link(True), local)

    def test_a_shared_link_tries_the_shared_endpoint_first(self):
        local, shared = llm_with(answering("locale")), llm_with(answering("condiviso"))
        chosen = LinkLLM(local, shared).for_link(False)
        self.assertEqual(chosen.chat([{"role": "user", "content": "x"}]), "condiviso")

    def test_without_a_shared_endpoint_everything_stays_local(self):
        local = llm_with(answering("locale"))
        self.assertIs(LinkLLM(local).for_link(False), local)


class FirstModelTests(unittest.TestCase):
    MODELS = ["auto", "groq/llama", "google/gemini-a", "google/gemini-b"]

    def handler(self, gone=()):
        used = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": m} for m in self.MODELS]})
            model = json.loads(request.content)["model"]
            used.append(model)
            if model in gone:
                return httpx.Response(404, text="model not found")
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        return handle, used

    def test_first_google_resolves_to_the_first_listed_google_model(self):
        handle, used = self.handler()
        llm = llm_with(handle, vision_model="first:google")
        llm.chat([{"role": "user", "content": "x"}], model="first:google")
        self.assertEqual(used, ["google/gemini-a"])

    def test_a_model_that_disappeared_is_skipped_for_the_next_one(self):
        handle, used = self.handler(gone={"google/gemini-a"})
        llm = llm_with(handle)
        self.assertEqual(llm.chat([{"role": "user", "content": "x"}], model="first:google"), "ok")
        self.assertEqual(used, ["google/gemini-a", "google/gemini-b"])
        llm.chat([{"role": "user", "content": "x"}], model="first:google")
        self.assertEqual(used[-1], "google/gemini-b")

    def test_no_model_for_the_provider_is_an_llm_error(self):
        handle, _ = self.handler()
        with self.assertRaises(LLMError):
            llm_with(handle).chat([{"role": "user", "content": "x"}], model="first:mistral")


class PrivateFlagTests(unittest.TestCase):
    def test_links_are_shared_unless_flagged_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.sqlite")
            state.add_link("https://a.example.com", "")
            state.add_link("https://b.example.com", "", private=True)
            self.assertFalse(state.link_private("https://a.example.com"))
            self.assertTrue(state.link_private("https://b.example.com"))
            self.assertFalse(state.link_private("https://unknown.example.com"))

    def test_an_existing_database_gets_the_private_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            State(path).db.execute("ALTER TABLE links DROP COLUMN private")
            reopened = State(path)
            reopened.add_link("https://c.example.com", "", private=True)
            self.assertTrue(reopened.link_private("https://c.example.com"))


if __name__ == "__main__":
    unittest.main()
