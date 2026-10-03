import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

import httpx

from digest.config import Config, ObsidianConfig
from digest.extract import highlights, summarize_link
from digest.models import Doc
from digest.obsidian import write_obsidian
from digest.recipes import (extract_recipe, ingredient_name, ingredients_from_text, quantity_in_source,
                            recipe_from_jsonld, recipe_tags)
from digest.report import link_block, write_reports
from digest.sources.links import fetch_links
from digest.state import State
from digest.tags import Vocabulary
from digest.web.server import create_server
from tests.test_web import make_service

SOURCE = ("Pasta al pomodoro. Ingredienti: 200 g spaghetti, 400 g pomodori maturi, basilico, sale q.b. "
          "Cuocere la pasta, preparare il sugo con i pomodori, unire il basilico. Consiglio: un pizzico di zucchero.")
LD_PAGE = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"Blog"},
 {"@type":["Recipe","Thing"],"name":"Cipolline in agrodolce","recipeCuisine":["Italiana"],"recipeYield":"4",
  "totalTime":"PT1H30M","recipeIngredient":["500 g di cipolline","3 cucchiai di aceto","q.b. sale","1 spicchio d'aglio"],
  "recipeInstructions":[{"@type":"HowToStep","text":"Pulire le cipolline &amp; sbollentarle."},
    {"@type":"HowToSection","itemListElement":[{"@type":"HowToStep","text":"2. Rosolare in padella."}]}]}]}
</script></head><body><article><p>%s</p></article></body></html>""" % ("Una ricetta molto buona. " * 30)

RECIPE_ANSWER = {
    "title": "Pasta al pomodoro", "cuisine": "italiana", "servings": None, "time": None,
    "ingredients": [{"quantity": "200 g", "item": "spaghetti"}, {"quantity": "999 g", "item": "pomodori maturi"},
                    {"quantity": "", "item": "basilico"}, "sale"],
    "steps": ["1. Cuocere la pasta.", "Preparare il sugo.", "Unire il basilico."], "tips": ["Un pizzico di zucchero."],
}
NO_LIST = "Una pagina qualsiasi senza alcun elenco di ingredienti, solo chiacchiere."
ANALYSIS_ANSWER = {"title": "T", "summary": "Un riassunto.", "key_points": ["k"], "tags": ["x"], "actions": ["a"],
                   "worth_it": "alta"}


class Scripted:
    """Fake LLM that answers by prompt: the recipe prompt gets the recipe, the link prompt gets the analysis."""

    def __init__(self, analysis: dict | None = None, recipe: dict | None = None):
        self.analysis = analysis if analysis is not None else dict(ANALYSIS_ANSWER)
        self.recipe = recipe if recipe is not None else dict(RECIPE_ANSWER)
        self.calls: list[str] = []

    def chat_json(self, system: str, user: str) -> dict:
        kind = "recipe" if "Estrai una ricetta" in system else "analysis"
        self.calls.append(kind)
        return self.recipe if kind == "recipe" else self.analysis


def doc(meta: dict | None = None, text: str = SOURCE) -> Doc:
    return Doc(source="link", id="u", title="Pagina", url="https://blog.example.com/pasta", text=text,
               meta={"platform": "web", **(meta or {})})


class JsonLdTests(unittest.TestCase):
    def test_recipe_is_found_inside_a_graph_with_a_list_type_and_normalised(self):
        recipe = recipe_from_jsonld(LD_PAGE)
        self.assertEqual((recipe["title"], recipe["cuisine"], recipe["servings"], recipe["time"]),
                         ("Cipolline in agrodolce", "Italiana", "4", "1 h 30 min"))
        self.assertEqual(recipe["ingredients"], ["500 g di cipolline", "3 cucchiai di aceto", "q.b. sale",
                                                 "1 spicchio d'aglio"])
        self.assertEqual(recipe["steps"], ["Pulire le cipolline & sbollentarle.", "Rosolare in padella."])

    def test_instruction_shapes(self):
        def steps(value) -> list[str]:
            page = ('<script type="application/ld+json">' + json.dumps(
                {"@type": "Recipe", "name": "x", "recipeIngredient": ["a"], "recipeInstructions": value}) + "</script>")
            return recipe_from_jsonld(page)["steps"]

        self.assertEqual(steps("Uno\nDue\n\nTre"), ["Uno", "Due", "Tre"])
        self.assertEqual(steps(["Uno", "Due"]), ["Uno", "Due"])
        self.assertEqual(steps("Un solo passaggio"), ["Un solo passaggio"])
        self.assertEqual(steps([{"@type": "HowToStep", "name": "Impasta"}]), ["Impasta"])

    def test_no_recipe_or_broken_json_gives_none(self):
        for page in ("<html></html>", '<script type="application/ld+json">{ nope</script>',
                     '<script type="application/ld+json">{"@type":"Article"}</script>',
                     '<script type="application/ld+json">{"@type":"Recipe","name":"empty"}</script>'):
            self.assertIsNone(recipe_from_jsonld(page))


class HeuristicTests(unittest.TestCase):
    def test_ingredient_names(self):
        cases = {
            "200 g di spaghetti": "spaghetti", "2 cucchiai di olio extravergine di oliva": "olio extravergine oliva",
            "1/2 cipolla": "cipolla", "q.b. sale": "sale", "1 spicchio d'aglio": "aglio",
            "400 g di pomodori maturi": "pomodori", "2-3 foglie di basilico": "basilico", "3 eggs, beaten": "eggs",
            "1 cup flour": "flour", "½ tazza di zucchero": "zucchero", "uova": "uova", "Un pizzico di sale": "sale",
            "150 g parmigiano (grattugiato)": "parmigiano",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(ingredient_name(line), expected)

    def test_quantity_guard(self):
        self.assertTrue(quantity_in_source("200 g", SOURCE))
        self.assertTrue(quantity_in_source("q.b.", SOURCE))
        self.assertTrue(quantity_in_source("", SOURCE))
        self.assertTrue(quantity_in_source("1,5 l", "acqua 1.5 l"))
        self.assertFalse(quantity_in_source("999 g", SOURCE))
        self.assertFalse(quantity_in_source("2 cucchiai", SOURCE))


class IngredientListTests(unittest.TestCase):
    def test_formats_of_an_ingredient_section(self):
        cases = {
            "markdown multi-line": ("**Ingredienti**:\n- 200 g di farina\n- 2 uova\n- 1 pizzico di sale\n\n**Preparazione**:\n1. Mescolare.",
                                    ["200 g di farina", "2 uova", "1 pizzico di sale"]),
            "numbered": ("Ingredienti\n1. farina 00\n2) uova\nProcedimento\nMescolare", ["farina 00", "uova"]),
            "one line": ("Ingredienti: 200 g spaghetti, 1 spicchio d'aglio, basilico. Preparazione: cuocere.",
                         ["200 g spaghetti", "1 spicchio d'aglio", "basilico"]),
            "q.b. keeps its dots": ("Ingredienti: pasta, sale q.b. Cuocere la pasta in acqua.", ["pasta", "sale q.b."]),
            "english": ("Ingredients: 2 cups flour; 1 egg\nDirections: mix", ["2 cups flour", "1 egg"]),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(ingredients_from_text(text), expected)

    def test_leading_quantities_are_never_mistaken_for_list_numbers(self):
        self.assertEqual(ingredients_from_text("Ingredienti:\n200 g farina\n1.5 l acqua\n3 uova"),
                         ["200 g farina", "1.5 l acqua", "3 uova"])

    def test_no_section_prose_or_messy_blocks_give_no_list(self):
        for text in ("Una ricetta senza elenco.", "Usiamo ingredienti freschi e di stagione per tutti i piatti.",
                     "Ingredienti: " + "una frase molto lunga che non è affatto un elenco di ingredienti " * 3):
            self.assertEqual(ingredients_from_text(text), [], text)


class ExtractRecipeTests(unittest.TestCase):
    def test_a_plain_ingredient_list_in_the_text_is_read_verbatim_and_wins_over_the_model(self):
        recipe = extract_recipe(Scripted(), Config(), doc())  # the model invented "999 g": the text says 400 g
        self.assertEqual([i["text"] for i in recipe["ingredients"]],
                         ["200 g spaghetti", "400 g pomodori maturi", "basilico", "sale q.b."])
        self.assertEqual([i["item"] for i in recipe["ingredients"]], ["spaghetti", "pomodori maturi", "basilico", "sale"])

    def test_the_text_list_completes_what_a_small_model_skipped(self):
        short = dict(RECIPE_ANSWER, ingredients=[{"quantity": "200 g", "item": "spaghetti"}])
        recipe = extract_recipe(Scripted(recipe=short), Config(), doc())
        self.assertEqual(len(recipe["ingredients"]), 4)

    def test_without_a_list_in_the_text_the_model_is_used_and_invented_quantities_are_dropped(self):
        text = "Pasta al pomodoro con spaghetti 200 g, pomodori maturi, basilico e sale. Cuocere la pasta."
        recipe = extract_recipe(Scripted(), Config(), doc(text=text))
        self.assertEqual([i["text"] for i in recipe["ingredients"]],
                         ["200 g spaghetti", "pomodori maturi", "basilico", "sale"])

    def test_ingredient_phrase_is_restored_as_written_in_the_source(self):
        text = "Ricetta. 1 spicchio d'aglio e 200 g di spaghetti, poi si cuoce."
        model = dict(RECIPE_ANSWER, ingredients=[{"quantity": "1 spicchio", "item": "aglio"},
                                                 {"quantity": "200 g", "item": "spaghetti"}])
        recipe = extract_recipe(Scripted(recipe=model), Config(), doc(text=text))
        self.assertEqual([i["text"] for i in recipe["ingredients"]], ["1 spicchio d'aglio", "200 g di spaghetti"])

    def test_model_reading_still_drops_a_quantity_not_in_the_source(self):
        text = "Ingredienti: pomodori e basilico, senza altro. Cuocere."
        model = dict(RECIPE_ANSWER, ingredients=[{"quantity": "999 g", "item": "pomodori"}])
        recipe = extract_recipe(Scripted(recipe=model), Config(), doc(text=text))
        self.assertNotIn("999", " ".join(i["text"] for i in recipe["ingredients"]))

    def test_string_null_is_not_shown_as_a_value(self):
        model = dict(RECIPE_ANSWER, servings="null", time="None", cuisine="null")
        recipe = extract_recipe(Scripted(recipe=model), Config(), doc())
        self.assertEqual((recipe["servings"], recipe["time"], recipe["cuisine"]), ("", "", ""))

    def test_steps_tips_and_facts_come_from_the_model_and_numbering_is_stripped(self):
        recipe = extract_recipe(Scripted(), Config(), doc())
        self.assertEqual(recipe["steps"], ["Cuocere la pasta.", "Preparare il sugo.", "Unire il basilico."])
        self.assertEqual(recipe["tips"], ["Un pizzico di zucchero."])
        self.assertEqual((recipe["cuisine"], recipe["servings"], recipe["time"]), ("italiana", "", ""))

    def test_jsonld_wins_over_the_model_and_the_model_fills_the_gaps(self):
        jsonld = recipe_from_jsonld(LD_PAGE)
        model = dict(RECIPE_ANSWER, title="Altro titolo", cuisine="francese", steps=["Passo del modello."],
                     ingredients=[{"quantity": "10 g", "item": "cipolline"}])
        recipe = extract_recipe(Scripted(recipe=model), Config(), doc(), jsonld)
        self.assertEqual((recipe["title"], recipe["cuisine"], recipe["servings"], recipe["time"]),
                         ("Cipolline in agrodolce", "Italiana", "4", "1 h 30 min"))
        self.assertEqual(recipe["steps"], jsonld["steps"])
        self.assertEqual([i["text"] for i in recipe["ingredients"]], jsonld["ingredients"])
        self.assertEqual(recipe["ingredients"][0]["item"], "cipolline")
        self.assertEqual(recipe["ingredients"][1]["item"], "aceto")
        self.assertEqual(recipe["tips"], ["Un pizzico di zucchero."])  # JSON-LD has no tips: the model's stay

    def test_jsonld_without_steps_uses_the_models_steps(self):
        jsonld = dict(recipe_from_jsonld(LD_PAGE), steps=[])
        self.assertEqual(extract_recipe(Scripted(), Config(), doc(), jsonld)["steps"][0], "Cuocere la pasta.")

    def test_null_like_cuisine_is_empty(self):
        for value in (None, "null", "N/A"):
            self.assertEqual(extract_recipe(Scripted(recipe=dict(RECIPE_ANSWER, cuisine=value)), Config(), doc())["cuisine"], "")


class RecipeTagTests(unittest.TestCase):
    RECIPE = {"cuisine": "Italiana", "ingredients": [
        {"text": "x", "item": "pomodori maturi"}, {"text": "x", "item": "spaghetti"},
        {"text": "x", "item": "olio extravergine di oliva"}, {"text": "x", "item": "basilico"}]}

    def test_one_cuisine_tag_plus_one_per_ingredient_reusing_the_vocabulary(self):
        tags, origin = recipe_tags(self.RECIPE, Vocabulary({"pomodoro": 2, "cucina-italiana": 3}))
        self.assertEqual(tags, ["cucina-italiana", "pomodoro", "spaghetti", "olio-extravergine-oliva", "basilico"])
        self.assertEqual(set(origin.values()), {"model"})

    def test_the_generic_ingredients_tag_never_appears(self):
        tags, _ = recipe_tags({"cuisine": "", "ingredients": [{"text": "x", "item": "ingredienti"}, {"text": "x", "item": "riso"}]},
                              Vocabulary())
        self.assertEqual(tags, ["riso"])

    def test_cuisine_prefix_is_not_doubled_and_missing_cuisine_adds_no_tag(self):
        self.assertEqual(recipe_tags({"cuisine": "cucina giapponese", "ingredients": []}, Vocabulary())[0],
                         ["cucina-giapponese"])
        self.assertNotIn("cucina-", " ".join(recipe_tags({"cuisine": "", "ingredients": [{"text": "x", "item": "riso"}]},
                                                         Vocabulary())[0]))

    def test_big_recipes_get_a_tag_for_every_ingredient_up_to_twenty(self):
        ingredients = [{"text": "x", "item": f"ingrediente{n}"} for n in range(30)]
        tags, _ = recipe_tags({"cuisine": "italiana", "ingredients": ingredients}, Vocabulary())
        self.assertEqual(len(tags), 20)

    def test_a_recipe_without_anything_still_has_a_tag(self):
        self.assertEqual(recipe_tags({"cuisine": "", "ingredients": []}, Vocabulary())[0], ["link", "ricetta"])


class SummarizeRecipeTests(unittest.TestCase):
    def test_model_flag_triggers_the_recipe_extraction_and_no_summary_is_kept(self):
        llm = Scripted(analysis=dict(ANALYSIS_ANSWER, is_recipe=True))
        result = summarize_link(llm, Config(), doc(), Vocabulary())
        self.assertEqual(llm.calls, ["analysis", "recipe"])
        self.assertEqual((result["kind"], result["summary"], result["key_points"], result["actions"]), ("recipe", "", [], []))
        self.assertEqual(result["title"], "Pasta al pomodoro")
        self.assertEqual(result["tags"][0], "cucina-italiana")
        self.assertIn("spaghetti", result["tags"])
        self.assertEqual(result["worth_it"], "alta")

    def test_various_truthy_flags(self):
        for flag in (True, "true", "True", "sì", "yes"):
            llm = Scripted(analysis=dict(ANALYSIS_ANSWER, is_recipe=flag))
            self.assertEqual(summarize_link(llm, Config(), doc())["kind"], "recipe", flag)

    def test_not_a_recipe_is_the_normal_analysis(self):
        for flag in (False, "false", None, "no"):
            llm = Scripted(analysis=dict(ANALYSIS_ANSWER, is_recipe=flag))
            result = summarize_link(llm, Config(), doc())
            self.assertEqual((result["kind"], result["summary"], llm.calls), ("link", "Un riassunto.", ["analysis"]), flag)

    def test_jsonld_recipe_skips_the_analysis_call(self):
        llm = Scripted()
        result = summarize_link(llm, Config(), doc({"recipe_jsonld": recipe_from_jsonld(LD_PAGE)}))
        self.assertEqual((llm.calls, result["kind"], result["title"]), (["recipe"], "recipe", "Cipolline in agrodolce"))

    def test_flagged_but_nothing_extracted_falls_back_to_the_normal_analysis(self):
        empty = dict(RECIPE_ANSWER, ingredients=[], steps=[])
        llm = Scripted(analysis=dict(ANALYSIS_ANSWER, is_recipe=True), recipe=empty)
        result = summarize_link(llm, Config(), doc(text=NO_LIST))
        self.assertEqual((result["kind"], result["summary"]), ("link", "Un riassunto."))

    def test_unusable_jsonld_still_produces_an_analysis(self):
        unusable = {"title": "x", "cuisine": "", "servings": "", "time": "", "ingredients": [], "steps": []}
        llm = Scripted(recipe=dict(RECIPE_ANSWER, ingredients=[], steps=[]))
        result = summarize_link(llm, Config(), doc({"recipe_jsonld": unusable}, text=NO_LIST))
        self.assertEqual((result["kind"], result["summary"], llm.calls), ("link", "Un riassunto.", ["recipe", "analysis"]))

    def test_recipes_never_reach_the_highlights(self):
        class Echo:
            calls = 0

            def chat_json(self, system, user):
                self.calls += 1
                self.user = user
                return {"highlights": ["x"]}

        echo = Echo()
        recipe = {"title": "Pasta", "summary": "", "kind": "recipe"}
        self.assertEqual(highlights(echo, Config(), [], [recipe]), [])
        self.assertEqual(echo.calls, 0)
        highlights(echo, Config(), [], [recipe, {"title": "Articolo", "summary": "Utile", "kind": "link"}])
        self.assertEqual(echo.user, "- [link] Articolo — Utile")


class RenderingTests(unittest.TestCase):
    LN = {"url": "https://blog.example.com/pasta", "platform": "web", "note": "da provare", "kind": "recipe",
          "title": "Pasta al pomodoro", "summary": "NON DEVE COMPARIRE", "key_points": ["nemmeno questo"],
          "actions": ["cerca altre ricette"], "tags": ["cucina-italiana", "pomodoro"],
          "recipe": {"title": "Pasta al pomodoro", "cuisine": "italiana", "servings": "4", "time": "30 min",
                     "ingredients": [{"text": "200 g spaghetti", "item": "spaghetti"}, {"text": "basilico", "item": "basilico"}],
                     "steps": ["Cuocere la pasta.", "Preparare il sugo."], "tips": ["Un pizzico di zucchero."]}}

    def test_block_lists_ingredients_numbered_steps_and_tips_without_summary_or_actions(self):
        block = link_block(self.LN)
        for expected in ("**Ingredienti**", "- 200 g spaghetti", "**Preparazione**", "1. Cuocere la pasta.",
                         "2. Preparare il sugo.", "**Consigli**", "- Un pizzico di zucchero.",
                         "italiana · 4 porzioni · 30 min", "> Tua nota: da provare", "#cucina-italiana #pomodoro"):
            self.assertIn(expected, block)
        for forbidden in ("NON DEVE COMPARIRE", "nemmeno questo", "Da provare", "cerca altre"):
            self.assertNotIn(forbidden, block)

    def test_optional_sections_are_left_out_when_empty(self):
        ln = dict(self.LN, recipe=dict(self.LN["recipe"], tips=[], cuisine="", servings="", time=""))
        block = link_block(ln)
        self.assertNotIn("**Consigli**", block)
        self.assertNotIn("porzioni", block)

    def test_normal_links_are_unchanged(self):
        block = link_block({"url": "https://a.example.com", "title": "T", "summary": "Riassunto", "key_points": ["k"],
                            "actions": ["a"], "tags": []})
        self.assertIn("Riassunto", block)
        self.assertIn("**Da provare:**", block)

    def test_obsidian_note_and_report_carry_the_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            cfg = Config(data_dir=str(Path(tmp) / "data"), reports_dir=str(Path(tmp) / "reports"), base_dir=Path(tmp),
                         obsidian=ObsidianConfig(enabled=True, vault=str(vault)))
            result = {"started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 1}, "items": [],
                      "links": [self.LN], "errors": [], "highlights": []}
            write_reports(result, cfg.reports_path)
            write_obsidian(result, cfg)
            for text in ((vault / "Digest" / "Link" / "Pasta al pomodoro.md").read_text(encoding="utf-8"),
                         (cfg.reports_path / "latest.md").read_text(encoding="utf-8")):
                self.assertIn("**Ingredienti**", text)
                self.assertNotIn("NON DEVE COMPARIRE", text)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service = make_service(self.tmp)
        self.cfg = self.service.cfg
        self.cfg.links.fetch_images = False
        self.cfg.links.use_ytdlp = False

    def tearDown(self):
        self._tmp.cleanup()

    def test_page_with_jsonld_recipe_reaches_the_summariser_as_exact_data(self):
        def handler(request):
            return httpx.Response(200, text=LD_PAGE, headers={"content-type": "text/html; charset=utf-8"})

        with State(self.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://blog.example.com/cipolline")
            docs, errors = fetch_links(self.cfg, state, None, httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(errors, [])
        self.assertEqual(docs[0].meta["recipe_jsonld"]["title"], "Cipolline in agrodolce")
        result = summarize_link(Scripted(), self.cfg, docs[0], Vocabulary())
        self.assertEqual([i["text"] for i in result["recipe"]["ingredients"]][:2], ["500 g di cipolline", "3 cucchiai di aceto"])

    def test_reprocess_puts_a_digested_link_back_in_the_queue_and_keeps_manual_tags(self):
        with State(self.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://a.example.com/x")
            state.link_done("https://a.example.com/x", "T", {"tags": ["old"], "tag_origin": {"old": "model"}})
        self.service.edit_tags("link", "https://a.example.com/x", ["mio"], [])
        self.service.reprocess_link("https://a.example.com/x")
        self.assertEqual(self.service.list_links()[0]["status"], "pending")
        with State(self.cfg.data_path / "state.sqlite") as state:
            state.link_done("https://a.example.com/x", "T2", {"tags": ["nuovo"], "tag_origin": {"nuovo": "model"}})
            self.assertEqual(state.link_analysis("https://a.example.com/x")["tags"], ["nuovo", "mio"])
        from digest.purge import NotFound

        with self.assertRaises(NotFound):
            self.service.reprocess_link("https://nope.example.com")

    def test_detail_endpoint_returns_the_recipe_and_404_for_unknown(self):
        ln = summarize_link(Scripted(analysis=dict(ANALYSIS_ANSWER, is_recipe=True)), self.cfg, doc(), Vocabulary())
        with State(self.cfg.data_path / "state.sqlite") as state:
            state.add_link(ln["url"])
            state.link_done(ln["url"], ln["title"], ln)
        server = create_server(self.service, port=0, dist_dir=self.tmp / "none")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", path)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            status, detail = get("/api/links/detail?url=" + ln["url"].replace(":", "%3A").replace("/", "%2F"))
            self.assertEqual(status, 200)
            self.assertEqual((detail["kind"], detail["recipe"]["steps"][0]), ("recipe", "Cuocere la pasta."))
            self.assertEqual(get("/api/links/detail?url=https%3A%2F%2Fnope.example.com")[0], 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
