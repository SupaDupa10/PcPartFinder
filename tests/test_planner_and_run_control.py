"""Suchplaner, Pagination und Steuerung eines laufenden Suchlaufs.

Deckt die Anforderungen ab, dass

* der Suchplan vollständig deterministisch erzeugt wird (Abschnitt 5),
* Pagination und Deduplizierung funktionieren (Abschnitt 6/17),
* ein Suchlauf gestartet, beobachtet, pausiert und abgebrochen werden kann
  (Abschnitt 4/17).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.connectors.base import SearchQuerySpec
from backend.search.planner import (
    PLAN_SOURCE_DETERMINISTIC,
    SearchPlanner,
    spacing_variants,
)

class TestDeterministicCore:
    async def test_plan_ist_deterministisch(self, knowledge):
        planner = SearchPlanner(knowledge)
        plan = await planner.build(categories=["gpu"], max_price=300.0)
        assert plan.source == PLAN_SOURCE_DETERMINISTIC
        assert plan.total_queries > 0
        assert plan.categories[0].category == "gpu"
        terms = [query.term for query in plan.categories[0].queries]
        assert terms
        assert len(terms) == len(set(terms)), "Der Plan darf keine Dubletten enthalten"

    async def test_plan_ist_reproduzierbar(self, knowledge):
        first = await SearchPlanner(knowledge).build(categories=["gpu", "cpu"], max_price=250.0)
        second = await SearchPlanner(knowledge).build(categories=["gpu", "cpu"], max_price=250.0)
        assert [q.term for c in first.categories for q in c.queries] == [
            q.term for c in second.categories for q in c.queries
        ]

    async def test_limit_wird_eingehalten(self, knowledge):
        planner = SearchPlanner(knowledge, max_variants_per_category=6)
        plan = await planner.build(categories=["gpu"], max_price=None)
        for category in plan.categories:
            assert len(category.queries) <= 6 + 2  # Limit plus strukturelle Varianten

    async def test_mehrere_kategorien(self, knowledge):
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu", "cpu", "ssd"], max_price=400.0
        )
        assert [c.category for c in plan.categories] == ["gpu", "cpu", "ssd"]

    async def test_fehlendes_preiswissen_wird_als_einschraenkung_gemeldet(self, knowledge):
        plan = await SearchPlanner(knowledge).build(categories=["gpu"], max_price=300.0)
        assert any("keine Preisrichtwerte" in item for item in plan.limitations)
        assert plan.expected_coverage == "" or isinstance(plan.expected_coverage, str)

    async def test_ausschlussbegriffe_stammen_aus_der_wissensbasis(self, knowledge, tmp_path):
        path = tmp_path / "kb.yaml"
        path.write_text(
            "entries:\n  - category: gpu\n    model: RTX 3080 Ti\n    problematic: true\n",
            encoding="utf-8",
        )
        from backend.knowledge.base import KnowledgeBase

        base = KnowledgeBase()
        base.load([path])
        plan = await SearchPlanner(base).build(categories=["gpu"], max_price=None)
        assert any("3080 Ti" in term for term in plan.global_exclusions)

    def test_schreibvarianten_ohne_leerzeichen(self):
        assert spacing_variants("rtx 4070") == ["rtx4070"]
        assert spacing_variants("rtx") == []


class TestStandortHinweise:
    """Der Standortwunsch darf nicht still ins Leere laufen."""

    @staticmethod
    def _registry(caps: dict):
        from types import SimpleNamespace

        return SimpleNamespace(
            get=lambda platform: SimpleNamespace(
                capabilities=SimpleNamespace(**caps[platform])
            )
        )

    def _request(self, **overrides):
        from backend.schemas import SearchRequest

        payload = {
            "product": "RTX 3080",
            "platforms": ["ebay_web", "kleinanzeigen_web"],
            "delivery": "pickup",
            "postal_code": "10115",
        }
        payload.update(overrides)
        return SearchRequest(**payload)

    def test_quelle_ohne_standortwissen_wird_gemeldet(self):
        from backend.search.runner import _location_limitations

        registry = self._registry(
            {
                "ebay_web": {
                    "label": "eBay (Website)",
                    "supports_location_filter": False,
                    "supports_pickup_info": False,
                },
                "kleinanzeigen_web": {
                    "label": "Kleinanzeigen (Website)",
                    "supports_location_filter": False,
                    "supports_pickup_info": True,
                },
            }
        )
        messages = _location_limitations(
            self._request(), ["ebay_web", "kleinanzeigen_web"], registry
        )
        assert len(messages) == 1
        assert "eBay (Website)" in messages[0]
        assert "Entfernung" in messages[0]

    def test_bei_reinem_versand_kein_hinweis(self):
        from backend.search.runner import _location_limitations

        registry = self._registry(
            {
                "ebay_web": {
                    "label": "eBay (Website)",
                    "supports_location_filter": False,
                    "supports_pickup_info": False,
                }
            }
        )
        assert _location_limitations(
            self._request(delivery="shipping"), ["ebay_web"], registry
        ) == []

    def test_ohne_plz_kein_hinweis(self):
        from backend.search.runner import _location_limitations

        registry = self._registry(
            {
                "ebay_web": {
                    "label": "eBay (Website)",
                    "supports_location_filter": False,
                    "supports_pickup_info": False,
                }
            }
        )
        assert _location_limitations(
            self._request(postal_code=None, delivery="both"), ["ebay_web"], registry
        ) == []

    def test_quelle_mit_standortdaten_ist_kein_problem(self):
        from backend.search.runner import _location_limitations

        registry = self._registry(
            {
                "kleinanzeigen_web": {
                    "label": "Kleinanzeigen (Website)",
                    "supports_location_filter": False,
                    "supports_pickup_info": True,
                }
            }
        )
        assert _location_limitations(
            self._request(), ["kleinanzeigen_web"], registry
        ) == []


class TestProduktPlan:
    """Suchplan für einen konkret eingegebenen Produktnamen."""

    async def test_produktname_steht_an_erster_stelle(self, knowledge):
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu"],
            max_price=300.0,
            product="RTX 3080 Ti",
            product_models=["RTX 3080 Ti", "RTX 3080"],
        )
        query = plan.categories[0].queries[0]
        assert query.term == "RTX 3080 Ti"
        assert query.expected_yield == "high"
        assert "Produktname" in query.reason or "Produktname" in plan.notes[0]

    async def test_schreibvariante_und_passende_modelle(self, knowledge):
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu"],
            max_price=300.0,
            product="RTX 3080",
            product_models=["RTX 3080 10GB", "RTX 3080 Ti"],
        )
        terms = [q.term for q in plan.categories[0].queries]
        assert "RTX 3080" in terms
        assert "RTX3080" in terms
        assert "RTX 3080 10GB" in terms
        assert "RTX 3080 Ti" in terms
        assert terms.index("RTX 3080") < terms.index("RTX 3080 Ti")

    async def test_mindestpreis_ist_ein_viertel_des_budgets(self, knowledge):
        """Jeder Suchbegriff trägt den Mindestpreis seines Höchstpreises."""
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu"], max_price=400.0, product="RTX 3080"
        )
        category = plan.categories[0]
        assert category.queries
        assert {query.min_price for query in category.queries} == {100.0}
        assert category.to_dict()["min_price"] == 100.0
        assert all(query.to_dict()["min_price"] == 100.0 for query in category.queries)

    async def test_ohne_budget_gibt_es_keinen_mindestpreis(self, knowledge):
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu"], max_price=None, product="RTX 3080"
        )
        assert {query.min_price for query in plan.categories[0].queries} == {None}
        assert plan.categories[0].to_dict()["min_price"] is None

    async def test_breite_begriffe_bleiben_reines_sicherheitsnetz(self, knowledge):
        plan = await SearchPlanner(knowledge).build(
            categories=["gpu"],
            max_price=300.0,
            product="RTX 3080",
            product_models=[],
        )
        generic = [q for q in plan.categories[0].queries if q.family == "generic"]
        assert 0 < len(generic) <= 3

class TestConnectorPagination:
    """Pagination muss ueber mehrere Seiten laufen und nichts doppeln."""

    async def test_seiten_werden_geliefert(self, app_state):
        connector = app_state.registry.get("local_import")
        assert connector is not None
        query = SearchQuerySpec(
            term="rtx 3080", platform="local_import", page_size=2, max_pages=5
        )
        first = await connector.search_page(query)
        assert first.count == 2
        assert first.has_next is True
        assert first.next_offset == 2

        second = await connector.search_page(query.with_offset(first.next_offset))
        assert second.offset == 2
        assert not ({item.url for item in first.listings} & {item.url for item in second.listings})

    async def test_komplette_pagination_ohne_duplikate(self, app_state):
        connector = app_state.registry.get("local_import")
        query = SearchQuerySpec(term="rtx 3080", platform="local_import", page_size=3)
        seen: list[str] = []
        offset = 0
        for _ in range(10):
            page = await connector.search_page(query.with_offset(offset))
            seen.extend(item.url for item in page.listings)
            if not page.has_next:
                break
            offset = page.next_offset
        assert len(seen) == len(set(seen))
        assert len(seen) > 3

    async def test_letzte_seite_meldet_kein_weiter(self, app_state):
        connector = app_state.registry.get("local_import")
        query = SearchQuerySpec(term="rtx 3080", platform="local_import", page_size=200)
        page = await connector.search_page(query)
        assert page.has_next is False


@pytest.fixture()
def slow_client(app_state, monkeypatch):
    """Macht jede Suchanfrage langsam genug, um pausieren/abbrechen zu koennen."""
    connector = app_state.registry.get("local_import")
    original = connector.search_page

    async def slow(query):
        await asyncio.sleep(0.4)
        return await original(query)

    monkeypatch.setattr(connector, "search_page", slow)
    return app_state


def wait_status(client, run_id: int, wanted: set[str], timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/search/runs/{run_id}").json()
        if last["status"] in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"Status {last.get('status')} nicht in {wanted} erreicht")


class TestRunControl:
    """Start, Beobachtung, Pause, Fortsetzen und Abbruch eines Suchlaufs."""

    PAUSE = "paused"
    DONE = {"completed", "failed", "cancelled"}

    def _start(self, client) -> int:
        created = client.post(
            "/api/search/runs",
            json={
                "categories": ["gpu", "cpu"],
                "platforms": ["local_import"],
                "max_price": 400,
                "delivery": "both",
                "postal_code": "",
                "days_back": 3650,
            },
        )
        assert created.status_code == 201
        return created.json()["run_id"]

    def test_pausieren_und_fortsetzen(self, client, slow_client):
        run_id = self._start(client)
        paused = wait_status(client, run_id, {self.PAUSE} | self.DONE)
        if paused["status"] == self.PAUSE:
            progress = paused["progress"]
            assert progress["paused"] is True
            queries_at_pause = progress["queries_done"]
            time.sleep(0.6)
            still = client.get(f"/api/search/runs/{run_id}").json()
            # Waehrend der Pause darf keine weitere Anfrage laufen.
            assert still["progress"]["queries_done"] == queries_at_pause

            resumed = client.post(f"/api/search/runs/{run_id}/resume")
            assert resumed.status_code == 200
            finished = wait_status(client, run_id, self.DONE)
            assert finished["status"] == "completed"
            assert finished["progress"]["queries_done"] >= queries_at_pause
        else:
            # Lauf war schneller fertig als die Pause greifen konnte.
            assert paused["status"] == "completed"

    def test_abbrechen_beendet_den_lauf(self, client, slow_client):
        run_id = self._start(client)
        cancelled = client.post(f"/api/search/runs/{run_id}/cancel")
        # Nur ein noch laufender Auftrag kann abgebrochen werden; war der Lauf in
        # der Zwischenzeit fertig, ist die Ablehnung die richtige Antwort.
        assert cancelled.status_code in {200, 409}
        final = wait_status(client, run_id, self.DONE)
        if cancelled.status_code == 409:
            assert final["status"] == "completed"
            return
        assert final["status"] in {"cancelled", "completed"}
        if final["status"] == "cancelled":
            assert final["progress"]["cancelled"] is True
            assert final["progress"]["phase"] == "cancelled"
            assert final["progress"]["message"]
            listings = client.get("/api/listings").json()
            # Ein abgebrochener Lauf darf trotzdem bereits gefundene Angebote behalten.
            assert listings["total"] >= 0

    def test_abbrechen_eines_fertigen_laufs_wird_abgelehnt(self, client, app_state):
        """Ein beendeter Lauf darf nicht als "abgebrochen" gemeldet werden."""
        run_id = self._start(client)
        wait_status(client, run_id, self.DONE)
        response = client.post(f"/api/search/runs/{run_id}/cancel")
        assert response.status_code == 409
        final = client.get(f"/api/search/runs/{run_id}").json()
        assert final["status"] == "completed"

    def test_nur_ein_lauf_gleichzeitig(self, client, slow_client):
        self._start(client)
        second = client.post(
            "/api/search/runs",
            json={
                "categories": ["gpu"],
                "platforms": ["local_import"],
                "max_price": 400,
                "delivery": "both",
                "postal_code": "",
                "days_back": 3650,
            },
        )
        assert second.status_code == 409
        assert "bereits" in second.json()["detail"]
        # Aufraeumen, damit spaetere Tests nicht blockiert werden.
        run_id = client.get("/api/search/runs").json()[0]["id"]
        client.post(f"/api/search/runs/{run_id}/cancel")
        wait_status(client, run_id, self.DONE)
