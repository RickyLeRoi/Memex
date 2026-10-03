import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from digest import __main__ as cli
from digest.areas import AreaCatalog, area_rules, collect_proposals, reclassify
from digest.config import Config, ObsidianConfig
from digest.extract import extract_items, summarize_link
from digest.llm import LLMError
from digest.models import Doc
from digest.obsidian import write_obsidian
from digest.purge import NotFound, Purger
from digest.report import write_reports
from digest.state import FALLBACK_AREA, State
from digest.tags import Vocabulary
from digest.web.server import create_server
from tests.test_web import make_service


class Scripted:
    def __init__(self, analysis=None, items=None, moves=None):
        self.analysis = analysis or {"title": "T", "summary": "s", "tags": ["x"]}
        self.items = items or []
        self.moves = moves
        self.systems: list[str] = []

    def chat_json(self, system, user):
        self.systems.append(system)
        if "NUOVA area: " in system:
            return {"moves": self.moves(user) if self.moves else []}
        if "Estrai una ricetta" in system:
            return {"title": "Pasta", "cuisine": "italiana", "ingredients": [{"quantity": "", "item": "pasta"}],
                    "steps": ["Cuocere."], "tips": []}
        return {"items": self.items} if self.items else self.analysis


def doc(text: str = "contenuto") -> Doc:
    return Doc(source="mail", id="m1", title="Mail", url="https://m/1", text=text, meta={"platform": "web"})


class TempState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = State(self.tmp / "state.sqlite")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()


class StateAreaTests(TempState):
    def test_the_users_starting_areas_are_seeded_once(self):
        names = [a["name"] for a in self.state.list_areas()]
        self.assertEqual(names[:7], ["ricette", "software", "posti", "progetti", "lavoro", "documenti", "salute"])
        self.assertEqual(names[-1], FALLBACK_AREA)
        self.assertTrue(self.state.get_area(FALLBACK_AREA)["system"])
        self.state.close()
        self.state = State(self.tmp / "state.sqlite")
        self.assertEqual(len(self.state.list_areas()), 8)

    def test_items_are_never_stored_without_an_area(self):
        self.state.save_items("r", [{"source": "mail", "kind": "task", "title": "t"},
                                    {"source": "mail", "kind": "task", "title": "u", "area": "lavoro", "area_origin": "model"}])
        rows = self.state.db.execute("SELECT title, area, area_origin FROM items ORDER BY id").fetchall()
        self.assertEqual(rows, [("t", "altro", "auto"), ("u", "lavoro", "model")])

    def test_migration_gives_the_fallback_area_to_what_existed_before(self):
        self.state.db.execute("INSERT INTO items(source, kind, title, tags, tag_origin) VALUES('mail','task','old','[\"a\"]','{}')")
        self.state.add_link("https://u")
        self.state.link_done("https://u", "T", {"tags": ["a"]})
        analysis = self.state.link_analysis("https://u")
        analysis.pop("area", None)
        self.state.set_link_analysis("https://u", analysis)
        self.state.db.commit()
        self.state.close()
        self.state = State(self.tmp / "state.sqlite")
        self.assertEqual(self.state.link_analysis("https://u")["area"], "altro")
        self.assertEqual(self.state.db.execute("SELECT area FROM items").fetchone()[0], "altro")

    def test_deleting_an_area_moves_its_entries_to_the_fallback_and_the_fallback_cannot_go(self):
        self.state.add_link("https://u")
        self.state.link_done("https://u", "T", {"tags": ["a"], "area": "salute", "area_origin": "model"})
        self.state.save_items("r", [{"source": "mail", "kind": "task", "title": "t", "area": "salute"}])
        self.assertEqual(self.state.area_counts()["salute"], 2)
        self.assertEqual(self.state.delete_area("salute"), 2)
        self.assertIsNone(self.state.get_area("salute"))
        self.assertEqual(self.state.link_analysis("https://u")["area"], "altro")
        self.state.delete_area(FALLBACK_AREA)
        self.assertIsNotNone(self.state.get_area(FALLBACK_AREA))

    def test_manual_area_survives_a_reprocess(self):
        self.state.add_link("https://u")
        self.state.link_done("https://u", "T", {"tags": ["a"], "area": "lavoro", "area_origin": "model"})
        self.state.set_link_area("https://u", "salute", "manual")
        self.state.link_done("https://u", "T", {"tags": ["b"], "area": "lavoro", "area_origin": "model"})
        analysis = self.state.link_analysis("https://u")
        self.assertEqual((analysis["area"], analysis["area_origin"]), ("salute", "manual"))


class CatalogTests(unittest.TestCase):
    def test_resolve_accepts_name_label_and_variants_but_only_active_areas(self):
        catalog = AreaCatalog()
        for raw in ("ricette", "Ricette", "RICETTE", "ricetta", "Posti", "posto"):
            self.assertIn(catalog.resolve(raw), ("ricette", "posti"), raw)
        self.assertIsNone(catalog.resolve("fitness"))
        self.assertIsNone(catalog.resolve(None))
        self.assertIsNone(catalog.resolve(""))

    def test_prompt_lists_every_active_area_with_its_description_and_the_rejected_ones(self):
        rows = [{"name": "ricette", "label": "Ricette", "description": "cibo", "status": "active"},
                {"name": "altro", "label": "Altro", "description": "resto", "status": "active"},
                {"name": "sport", "label": "Sport", "description": "", "status": "rejected"},
                {"name": "viaggi", "label": "Viaggi", "description": "", "status": "proposed"}]
        rules = area_rules(AreaCatalog(rows))
        self.assertIn("- ricette: cibo", rules)
        self.assertIn("- altro: resto", rules)
        self.assertNotIn("viaggi: ", rules)
        self.assertIn("non riproporle: sport", rules)

    def test_valid_area_has_no_proposal(self):
        self.assertEqual(AreaCatalog().finalize("lavoro", {"name": "viaggi", "why": "x"}, "t"), ("lavoro", "model", None))

    def test_missing_or_unknown_area_falls_back_and_never_stays_empty(self):
        self.assertEqual(AreaCatalog().finalize(None, None, "t"), ("altro", "auto", None))
        self.assertEqual(AreaCatalog().finalize("", "garbage", "t")[:2], ("altro", "auto"))

    def test_an_unknown_area_name_written_by_the_model_becomes_a_proposal(self):
        area, origin, proposal = AreaCatalog().finalize("Fitness", None, "Palestra a Milano")
        self.assertEqual((area, origin), ("altro", "auto"))
        self.assertEqual((proposal["name"], proposal["label"], proposal["example"]), ("fitness", "Fitness", "Palestra a Milano"))

    def test_an_explicit_proposal_with_a_reason(self):
        area, origin, proposal = AreaCatalog().finalize("altro", {"name": "Animali domestici", "why": "ricorrente"}, "Cane")
        self.assertEqual((area, origin, proposal["name"], proposal["why"]), ("altro", "model", "animali-domestici", "ricorrente"))

    def test_bad_proposals_are_dropped(self):
        catalog = AreaCatalog([{"name": "altro", "label": "Altro", "description": "", "status": "active"},
                               {"name": "sport", "label": "Sport", "description": "", "status": "rejected"}])
        for wanted in ("", "   ", "x" * 50, "una due tre quattro", "sport", "altro", None, 42):
            self.assertIsNone(catalog.finalize("altro", {"name": wanted}, "t")[2], wanted)

    def test_a_proposal_merges_with_the_same_one_in_plural_or_singular_form(self):
        rows = [{"name": "altro", "label": "Altro", "description": "", "status": "active"},
                {"name": "animali", "label": "Animali", "description": "", "status": "proposed"}]
        self.assertEqual(AreaCatalog(rows).finalize("altro", {"name": "animale"}, "t")[2]["name"], "animali")


class ExtractionTests(unittest.TestCase):
    ITEM = {"kind": "task", "title": "Iscrivermi in palestra", "ref": 1, "area": "salute"}

    def test_prompt_contains_the_areas_and_items_get_one(self):
        llm = Scripted(items=[self.ITEM])
        items, _, _ = extract_items(llm, Config(), [doc()], Vocabulary(), AreaCatalog())
        self.assertIn("- salute:", llm.systems[0])
        self.assertIn("Se nessuna calza usa \"altro\"", llm.systems[0])
        self.assertEqual((items[0]["area"], items[0]["area_origin"]), ("salute", "model"))

    def test_item_without_a_valid_area_gets_the_fallback_and_may_carry_a_proposal(self):
        item = dict(self.ITEM, area="altro", area_proposal={"name": "fitness", "why": "ricorrente"})
        items, _, _ = extract_items(Scripted(items=[item]), Config(), [doc()], Vocabulary(), AreaCatalog())
        self.assertEqual((items[0]["area"], items[0]["area_proposal"]["name"]), ("altro", "fitness"))

    def test_link_analysis_gets_an_area(self):
        llm = Scripted(analysis={"title": "T", "summary": "s", "tags": ["x"], "area": "software"})
        result = summarize_link(llm, Config(), doc(), Vocabulary(), AreaCatalog())
        self.assertEqual((result["area"], result["area_origin"]), ("software", "model"))
        self.assertNotIn("area_proposal", result)

    def test_a_recipe_goes_to_the_recipes_area_unless_the_user_removed_it(self):
        llm = Scripted(analysis={"title": "T", "summary": "s", "tags": ["x"], "is_recipe": True, "area": "software"})
        self.assertEqual(summarize_link(llm, Config(), doc(), Vocabulary(), AreaCatalog())["area"], "ricette")
        without = AreaCatalog([a for a in [{"name": "altro", "label": "Altro", "description": "", "status": "active"}]])
        result = summarize_link(llm, Config(), doc(), Vocabulary(), without)
        self.assertEqual((result["area"], result["area_origin"]), ("altro", "auto"))

    def test_proposals_are_collected_out_of_the_entries(self):
        with tempfile.TemporaryDirectory() as tmp, State(Path(tmp) / "s.sqlite") as state:
            entry = {"title": "x", "area_proposal": {"name": "fitness", "label": "Fitness", "why": "w", "example": "Palestra"}}
            self.assertEqual(collect_proposals(state, [entry, {"title": "no proposal"}]), 1)
            self.assertNotIn("area_proposal", entry)
            proposal = state.get_area("fitness")
            self.assertEqual((proposal["status"], proposal["proposals"], proposal["evidence"]), ("proposed", 1, ["Palestra"]))

    def test_proposals_strengthen_but_active_and_rejected_areas_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp, State(Path(tmp) / "s.sqlite") as state:
            for n in range(8):
                state.add_area_proposal("fitness", "Fitness", "w", f"esempio {n}")
            area = state.get_area("fitness")
            self.assertEqual((area["proposals"], len(area["evidence"])), (8, 5))
            state.set_area_status("fitness", "rejected")
            state.add_area_proposal("fitness", "Fitness", "w", "again")
            state.add_area_proposal("salute", "Salute", "w", "again")
            self.assertEqual((state.get_area("fitness")["status"], state.get_area("fitness")["proposals"]), ("rejected", 8))
            self.assertEqual(state.get_area("salute")["status"], "active")


class ReclassifyTests(TempState):
    def setUp(self):
        super().setUp()
        self.cfg = Config()
        self.state.save_area("fitness", "Fitness", "palestra, corsa, allenamento", "#22c55e")
        self.state.save_items = self.state.save_items
        self.state.save_items("r", [
            {"source": "mail", "kind": "task", "title": "Iscrizione palestra", "details": "abbonamento palestra", "area": "altro"},
            {"source": "mail", "kind": "task", "title": "Rinnovo assicurazione", "details": "auto", "area": "documenti"},
            {"source": "mail", "kind": "info", "title": "Allenamento a casa", "details": "palestra", "area": "salute",
             "area_origin": "manual"},
        ])
        self.state.db.execute("UPDATE items SET area_origin='manual' WHERE title='Allenamento a casa'")
        self.state.add_link("https://corsa")
        self.state.link_done("https://corsa", "Piano di corsa per la palestra", {"summary": "palestra e corsa", "tags": ["corsa"],
                                                                                "area": "altro", "area_origin": "auto"})
        self.state.add_link("https://fisco")
        self.state.link_done("https://fisco", "Dichiarazione dei redditi", {"summary": "tasse", "tags": ["tasse"],
                                                                           "area": "lavoro", "area_origin": "model"})
        self.state.db.commit()
        self.llm = Scripted(moves=lambda listing: [int(line[1:line.index("]")]) for line in listing.splitlines()
                                                    if "palestra" in line.lower()])

    def areas(self) -> dict[str, str]:
        out = {title: area for title, area in self.state.db.execute("SELECT title, area FROM items")}
        for url, in self.state.db.execute("SELECT url FROM links"):
            out[url] = self.state.link_analysis(url)["area"]
        return out

    def test_only_what_clearly_belongs_moves_into_the_new_area(self):
        moved = reclassify(self.llm, self.cfg, self.state, "fitness")
        areas = self.areas()
        self.assertEqual(moved, 2)
        self.assertEqual((areas["Iscrizione palestra"], areas["https://corsa"]), ("fitness", "fitness"))
        self.assertEqual((areas["Rinnovo assicurazione"], areas["https://fisco"]), ("documenti", "lavoro"))

    def test_areas_chosen_by_hand_are_never_overwritten(self):
        reclassify(self.llm, self.cfg, self.state, "fitness")
        self.assertEqual(self.areas()["Allenamento a casa"], "salute")

    def test_the_model_cannot_move_things_anywhere_else_or_use_invalid_numbers(self):
        llm = Scripted(moves=lambda listing: [0, 99, "x", -1])
        self.assertEqual(reclassify(llm, self.cfg, self.state, "fitness"), 0)
        self.assertEqual(self.areas()["Iscrizione palestra"], "altro")

    def test_running_it_again_is_harmless(self):
        reclassify(self.llm, self.cfg, self.state, "fitness")
        before = self.areas()
        self.assertEqual(reclassify(self.llm, self.cfg, self.state, "fitness"), 0)
        self.assertEqual(self.areas(), before)

    def test_batches_and_progress_and_a_failing_batch_does_not_stop_the_rest(self):
        class Flaky(Scripted):
            calls = 0

            def chat_json(self, system, user):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise LLMError("boom")
                return super().chat_json(system, user)

        messages: list[str] = []
        flaky = Flaky(moves=lambda listing: [int(line[1:line.index("]")]) for line in listing.splitlines()
                                              if "palestra" in line.lower()])
        reclassify(flaky, self.cfg, self.state, "fitness", batch_size=1, progress=messages.append)
        self.assertGreater(Flaky.calls, 2)
        self.assertTrue(any("boom" in m for m in messages))
        self.assertTrue(any("spostate" in m for m in messages))

    def test_the_prompt_names_the_new_area_and_the_others(self):
        reclassify(self.llm, self.cfg, self.state, "fitness")
        self.assertIn("«Fitness» (palestra, corsa, allenamento)", self.llm.systems[0])
        self.assertIn("ricette", self.llm.systems[0])

    def test_only_active_areas_can_be_the_target(self):
        self.state.add_area_proposal("viaggi", "Viaggi", "w", "x")
        for name in ("viaggi", "inesistente"):
            with self.assertRaises(ValueError):
                reclassify(self.llm, self.cfg, self.state, name)


class ServiceAreaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service = make_service(self.tmp)
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.add_link("https://a")
            state.link_done("https://a", "A", {"tags": ["t"], "area": "software", "area_origin": "model"})
            state.save_items("r", [
                {"source": "mail", "kind": "task", "title": "One", "ref_url": "https://m/1", "area": "lavoro"},
                {"source": "mail", "kind": "task", "title": "Two", "ref_url": "https://m/1", "area": "lavoro"},
                {"source": "mail", "kind": "task", "title": "Three", "ref_url": "https://m/1", "area": "salute"},
            ])
            state.add_area_proposal("fitness", "Fitness", "ricorrente", "Palestra")

    def tearDown(self):
        self._tmp.cleanup()

    def test_listing_has_counts_proposals_and_evidence(self):
        areas = {a["name"]: a for a in self.service.areas()}
        self.assertEqual((areas["lavoro"]["count"], areas["software"]["count"]), (2, 1))
        self.assertEqual((areas["fitness"]["status"], areas["fitness"]["evidence"]), ("proposed", ["Palestra"]))

    def test_create_is_immediate_and_validated(self):
        area = self.service.create_area("Auto e moto", "veicoli", "#112233")
        self.assertEqual((area["name"], area["status"]), ("auto-e-moto", "active"))
        for args in (("Auto e moto", "", "#112233"), ("", "", "#112233"), ("Nuova", "", "rosso"), ("x" * 60, "", "#112233")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.service.create_area(*args)

    def test_a_rejected_name_can_be_added_by_hand(self):
        self.service.reject_area("fitness")
        self.assertEqual(self.service.create_area("Fitness", "", "#22c55e")["status"], "active")

    def test_update_and_the_fallback_area_rules(self):
        area = self.service.update_area("lavoro", {"label": "Lavoro e clienti", "color": "#010203", "exclude_from_vault": True})
        self.assertEqual((area["label"], area["color"], area["exclude_from_vault"]), ("Lavoro e clienti", "#010203", True))
        with self.assertRaises(ValueError):
            self.service.update_area("altro", {"exclude_from_vault": True})
        with self.assertRaises(ValueError):
            self.service.update_area("lavoro", {"color": "nope"})
        with self.assertRaises(NotFound):
            self.service.update_area("nope", {})

    def test_delete_moves_entries_and_protects_the_fallback(self):
        self.assertEqual(self.service.delete_area("lavoro"), {"moved": 2})
        with self.assertRaises(ValueError):
            self.service.delete_area("altro")
        with self.assertRaises(NotFound):
            self.service.delete_area("nope")

    def test_reject_only_applies_to_proposals(self):
        self.service.reject_area("fitness")
        with self.assertRaises(NotFound):
            self.service.reject_area("lavoro")

    def test_approve_activates_and_starts_the_reevaluation_job(self):
        with mock.patch.object(self.service, "_start_job", return_value=SimpleNamespace(id="abc")) as start:
            job = self.service.approve_area("fitness", {"label": "Fitness e sport", "color": "#22c55e", "description": "palestra"})
        start.assert_called_once_with(["area:fitness"], ["reclassify", "--area", "fitness"])
        self.assertEqual(job.id, "abc")
        area = {a["name"]: a for a in self.service.areas()}["fitness"]
        self.assertEqual((area["status"], area["label"], area["description"]), ("active", "Fitness e sport", "palestra"))

    def test_approve_is_refused_while_a_job_runs_and_for_non_proposals(self):
        with mock.patch.object(self.service, "running_job", return_value=object()), self.assertRaises(RuntimeError):
            self.service.approve_area("fitness", {})
        self.assertEqual({a["name"]: a for a in self.service.areas()}["fitness"]["status"], "proposed")
        with self.assertRaises(NotFound):
            self.service.approve_area("lavoro", {})

    def test_manual_assignment_for_links_items_and_docs(self):
        self.service.assign_area("link", "https://a", "progetti")
        self.service.assign_area("doc", "https://m/1", "salute")
        node = {n["id"]: n for n in self.service.graph()["nodes"]}
        self.assertEqual((node["https://a"]["area"], node["https://a"]["area_origin"]), ("progetti", "manual"))
        self.assertEqual({n["area"] for n in node.values() if n["type"] == "item"}, {"salute"})
        with self.assertRaises(ValueError):
            self.service.assign_area("link", "https://a", "fitness")  # a proposal is not an area yet
        with self.assertRaises(ValueError):
            self.service.assign_area("link", "https://a", "inesistente")
        with self.assertRaises(NotFound):
            self.service.assign_area("link", "https://nope", "lavoro")

    def test_graph_and_stats_carry_areas(self):
        nodes = {n["id"]: n for n in self.service.graph()["nodes"]}
        self.assertEqual(nodes["https://a"]["area"], "software")
        self.assertEqual(nodes["https://m/1"]["area"], "lavoro")
        self.assertEqual(self.service.stats()["by_area"], {"software": 1, "lavoro": 2, "salute": 1})

    def test_areas_never_create_edges(self):
        self.assertEqual([e for e in self.service.graph()["edges"] if e["type"] not in ("ref",)], [])


class AreaRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self._tmp.name))
        with State(self.service.cfg.data_path / "state.sqlite") as state:
            state.add_area_proposal("fitness", "Fitness", "w", "x")
        self.server = create_server(self.service, port=0, dist_dir=Path(self._tmp.name) / "none")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else None)

    def test_crud_flow(self):
        self.assertEqual(self.call("GET", "/api/areas")[0], 200)
        status, area = self.call("POST", "/api/areas", {"label": "Auto", "color": "#102030"})
        self.assertEqual((status, area["name"]), (200, "auto"))
        self.assertEqual(self.call("POST", "/api/areas", {"label": "Auto", "color": "#102030"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/areas/auto", {"label": "Auto e moto"})[1]["label"], "Auto e moto")
        self.assertEqual(self.call("DELETE", "/api/areas/auto")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/areas/altro")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/areas/nope")[0], 404)

    def test_proposal_flow_and_errors(self):
        self.assertEqual(self.call("POST", "/api/areas/fitness/reject", {})[0], 200)
        self.assertEqual(self.call("POST", "/api/areas/fitness/reject", {})[0], 404)
        with mock.patch.object(self.service, "approve_area", side_effect=RuntimeError("busy")):
            self.assertEqual(self.call("POST", "/api/areas/fitness/approve", {})[0], 409)
        with mock.patch.object(self.service, "approve_area", return_value=SimpleNamespace(id="abc123")):
            self.assertEqual(self.call("POST", "/api/areas/fitness/approve", {}), (200, {"job": "abc123"}))

    def test_assign_and_vault_purge_routes(self):
        self.assertEqual(self.call("POST", "/api/areas/assign", {"kind": "link", "id": "https://nope", "area": "lavoro"})[0], 404)
        self.assertEqual(self.call("POST", "/api/areas/assign", {"kind": "link", "id": "x", "area": "fitness"})[0], 400)
        self.assertEqual(self.call("POST", "/api/areas/lavoro/vault-purge", {})[0], 200)
        self.assertEqual(self.call("POST", "/api/areas/nope/vault-purge", {})[0], 404)

    def test_odd_paths_are_not_area_routes(self):
        for path in ("/api/areas/../x", "/api/areas/UPPER", "/api/areas/a/b/c", "/api/areas/x/unknown"):
            self.assertEqual(self.call("PUT", path, {})[0], 404, path)


class VaultExclusionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp,
                          obsidian=ObsidianConfig(enabled=True, vault=str(self.vault)))
        self.state = State(self.cfg.data_path / "state.sqlite")
        self.media = self.cfg.data_path / "media"
        self.media.mkdir(parents=True)
        self.png = "a" * 40 + ".png"
        (self.media / self.png).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def result(self) -> dict:
        def ln(url, title, area, image=None):
            return {"url": url, "platform": "web", "note": "", "title": title, "summary": f"About {title}", "key_points": [],
                    "tags": ["t"], "actions": [], "worth_it": "media", "area": area, "image": image}

        item = lambda title, area: {"kind": "task", "title": title, "details": "d", "owner": None, "due": None,  # noqa: E731
                                    "priority": "media", "source": "mail", "ref_title": "Doc", "ref_url": "https://m/1",
                                    "mine": False, "area": area}
        return {"started": datetime(2026, 10, 2, 9, 30).astimezone(), "stats": {"link": 2},
                "items": [item("Visita medica", "salute"), item("Riunione cliente", "lavoro")],
                "links": [ln("https://h", "Esami del sangue", "salute", self.png), ln("https://w", "Articolo di lavoro", "lavoro")],
                "errors": [], "highlights": ["Un evidenza"]}

    def vault_text(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8") for p in self.vault.rglob("*.md"))

    def test_nothing_changes_when_no_area_is_excluded(self):
        write_obsidian(self.result(), self.cfg)
        text = self.vault_text()
        for expected in ("Esami del sangue", "Articolo di lavoro", "Visita medica", "Riunione cliente", "Un evidenza"):
            self.assertIn(expected, text)

    def test_an_excluded_area_leaves_no_note_line_image_highlight_or_stat(self):
        write_obsidian(self.result(), self.cfg, {"salute"})
        text = self.vault_text()
        self.assertNotIn("Esami del sangue", text)
        self.assertNotIn("Visita medica", text)
        self.assertNotIn("Un evidenza", text)  # free text cannot be attributed: dropped
        self.assertNotIn("link: 2", text)
        self.assertIn("Articolo di lavoro", text)
        self.assertIn("Riunione cliente", text)
        self.assertFalse((self.vault / "Digest" / "Link" / "media" / self.png).exists())
        self.assertFalse((self.vault / "Digest" / "Link" / "Esami del sangue.md").exists())

    def test_nothing_at_all_is_written_when_everything_is_excluded(self):
        write_obsidian(self.result(), self.cfg, {"salute", "lavoro"})
        self.assertEqual(list(self.vault.rglob("*.md")), [])

    def test_cleanup_removes_what_is_already_in_the_vault_and_only_that_area(self):
        result = self.result()
        for ln in result["links"]:
            self.state.add_link(ln["url"])
            self.state.link_done(ln["url"], ln["title"], ln)
        self.state.save_items("r", result["items"])
        write_reports(result, self.cfg.reports_path)
        write_obsidian(result, self.cfg)
        self.assertIn("Esami del sangue", self.vault_text())
        outcome = Purger(self.cfg, self.state).vault_cleanup("salute")
        self.assertEqual(outcome["errors"], [])
        text = self.vault_text()
        self.assertNotIn("Esami del sangue", text)
        self.assertNotIn("Visita medica", text)
        self.assertIn("Articolo di lavoro", text)
        self.assertIn("Riunione cliente", text)
        self.assertFalse((self.vault / "Digest" / "Link" / "media" / self.png).exists())
        # the vault is the only thing touched
        self.assertTrue((self.media / self.png).is_file())
        self.assertIsNotNone(self.state.link_row("https://h"))
        self.assertEqual(len(self.state.items_for_ref("https://m/1")), 2)
        self.assertIn("Esami del sangue", (self.cfg.reports_path / "latest.md").read_text(encoding="utf-8"))

    def test_cleanup_without_a_vault_does_nothing(self):
        self.cfg.obsidian.vault = ""
        self.assertEqual(Purger(self.cfg, self.state).vault_cleanup("salute"), {"edited": [], "errors": []})


class PipelineProposalTests(TempState):
    def test_a_proposal_goes_to_the_approval_queue_and_not_into_the_stored_analysis(self):
        cfg = Config(data_dir=str(self.tmp / "data"), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp)
        self.state.add_link("https://m/1")
        llm = Scripted(analysis={"title": "Palestra", "summary": "s", "tags": ["x"], "area": "altro",
                                 "area_proposal": {"name": "Fitness", "why": "ricorrente"}})
        ln = summarize_link(llm, cfg, doc(), Vocabulary(), AreaCatalog())
        collect_proposals(self.state, [ln])
        self.state.link_done("https://m/1", ln["title"], ln)
        self.assertEqual(self.state.get_area("fitness")["status"], "proposed")
        self.assertNotIn("area_proposal", self.state.link_analysis("https://m/1"))
        self.assertEqual(self.state.link_analysis("https://m/1")["area"], "altro")

    def test_the_cli_command_refuses_an_area_that_is_not_active(self):
        cfg = Config(data_dir=str(self.tmp), reports_dir=str(self.tmp / "reports"), base_dir=self.tmp)
        self.state.add_area_proposal("viaggi", "Viaggi", "w", "x")
        self.state.close()
        self.assertEqual(cli.cmd_reclassify(cfg, SimpleNamespace(area="viaggi")), 2)
        self.state = State(self.tmp / "state.sqlite")


if __name__ == "__main__":
    unittest.main()
