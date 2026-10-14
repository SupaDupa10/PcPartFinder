"""Suchmechanik: Seitengröße, parallele Seiten, Suchansichten und Limits.

Geprüft wird die Arbeit des Suchlaufs selbst - mit einem Connector-Stub, damit
weder Netz noch Browser nötig sind. Der Stub zählt mit, welche Anfragen
tatsächlich gestellt werden und wie viele davon gleichzeitig laufen.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.config import RuntimeSettings
from backend.connectors.base import (
    ConnectorCapabilities,
    ListingConnector,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)
from backend.domain.hardware import HardwareResolver
from backend.models import RUN_RUNNING, SearchRun
from backend.schemas import SearchRequest
from backend.search.ingest import OfferIngestor
from backend.search.planner import CategoryPlan, FinalSearchPlan, PlannedQuery
from backend.search.runner import (
    RunControl,
    SearchRunner,
    _price_reason_key,
    build_ingest_options,
)


class _StubConnector(ListingConnector):
    """Connector ohne Netz: liefert 240er-Seiten und protokolliert die Abrufe.

    Er tritt im Test als eBay-Website-Connector auf, damit die Anfrage gültig
    bleibt - angesprochen wird er nur über die Registry des Tests.
    """

    platform = "ebay_web"
    sort_variants = ("newest",)

    def __init__(
        self,
        *,
        page_size: int = 240,
        total: int = 1200,
        max_results: int = 1200,
        delay: float = 0.0,
        report_total: bool = True,
    ) -> None:
        self._page_size = page_size
        self._total = total
        self._max_results = max_results
        self._delay = delay
        self._report_total = report_total
        self.calls: list[SearchQuerySpec] = []
        self.running = 0
        self.max_concurrent = 0

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            platform=self.platform,
            label="Stub",
            enabled=True,
            configured=True,
            available=True,
            supports_search=True,
            supports_pagination=True,
            page_size_max=self._page_size,
            max_results_per_query=self._max_results,
        )

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        self.calls.append(query)
        self.running += 1
        self.max_concurrent = max(self.max_concurrent, self.running)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            start = max(0, query.offset)
            limit = max(0, min(query.page_size, self._total - start))
            listings = [
                RawListing(
                    platform=self.platform,
                    platform_listing_id=f"stub-{start + index}",
                    url=f"https://example.invalid/stub/{start + index}",
                    title=f"ASUS RTX 3080 TUF Gaming 10GB Grafikkarte Nummer {start + index}",
                    description="Grafikkarte mit Rechnung.",
                    price=200.0,
                    buying_options=["FIXED_PRICE"],
                    platform_format="FIXED_PRICE",
                )
                for index in range(limit)
            ]
            reached_end = start + limit >= self._total
            return SearchPage(
                listings=listings,
                offset=start,
                limit=query.page_size,
                total_reported=self._total if self._report_total else None,
                has_next=not reached_end,
                next_offset=None if reached_end else start + limit,
                meta={"url": f"stub://{start}", "sort": query.sort},
            )
        finally:
            self.running -= 1


@pytest.fixture()
def abruf(app_state, db_session):
    """Führt eine echte Suchanfrage gegen einen registrierten Stub aus."""

    def build(stub: _StubConnector):
        app_state.registry.build()["ebay_web"] = stub
        request = SearchRequest(platforms=["ebay_web"], categories=["gpu"])
        runtime = RuntimeSettings()
        run = SearchRun(
            status=RUN_RUNNING,
            request=request.model_dump(),
            progress={},
            errors=[],
            limitations=[],
        )
        db_session.add(run)
        db_session.commit()
        ingestor = OfferIngestor(
            db_session,
            knowledge=app_state.knowledge,
            resolver=HardwareResolver(app_state.knowledge),
            options=build_ingest_options(runtime, request),
        )
        runner = SearchRunner(
            settings=app_state.settings,
            registry=app_state.registry,
            knowledge=app_state.knowledge,
        )

        async def execute(spec: SearchQuerySpec, control: RunControl | None = None) -> dict:
            return await runner._execute_query(
                db_session,
                run,
                control or RunControl(run_id=run.id),
                runtime,
                spec,
                ingestor,
                request,
            )

        return execute, runtime

    return build


def _spec(**overrides) -> SearchQuerySpec:
    params = {"term": "grafikkarte", "platform": "ebay_web", "page_size": 50}
    params.update(overrides)
    return SearchQuerySpec(**params)


class TestSeitenabruf:
    """Die Zahlen sind klein gehalten; geprüft wird die Mechanik, nicht die Menge."""

    async def test_seitengroesse_wird_angehoben(self, abruf):
        stub = _StubConnector(page_size=60, total=180, max_results=1200)
        execute, _runtime = abruf(stub)
        outcome = await execute(_spec())  # Anfrage wollte 50 Treffer je Seite
        # Die Seitengröße richtet sich nach der Plattform (hier 60).
        assert {call.page_size for call in stub.calls} == {60}
        # 180 Treffer / 60 je Seite = 3 Seiten, alle abgerufen.
        assert len(stub.calls) == 3
        assert outcome["pages"] == 3
        assert outcome["results_returned"] == 180
        assert outcome["results_usable"] == 180
        assert outcome["truncated"] is False

    async def test_limit_der_plattform_schneidet_ab_und_meldet_es(self, abruf):
        stub = _StubConnector(page_size=60, total=180, max_results=60)
        execute, _runtime = abruf(stub)
        outcome = await execute(_spec())
        # Nur eine Seite laut Obergrenze der Plattform - der Rest ist abgeschnitten.
        assert outcome["pages"] == 1
        assert outcome["truncated"] is True
        assert outcome["offset_reached"] == 60
        assert outcome["reported_total"] == 180

    async def test_bekannte_trefferzahl_erlaubt_parallele_seiten(self, abruf):
        stub = _StubConnector(page_size=60, total=1200, max_results=1200, delay=0.02)
        execute, _runtime = abruf(stub)
        await execute(_spec())
        assert stub.max_concurrent >= 2

    async def test_ohne_trefferzahl_keine_ueberfluessigen_abrufe(self, abruf):
        stub = _StubConnector(page_size=60, total=180, max_results=1200, report_total=False)
        execute, _runtime = abruf(stub)
        outcome = await execute(_spec())
        # Ohne Gesamttrefferzahl wird Seite für Seite vorgegangen: keine
        # Anfrage für eine Seite, die es vielleicht nicht gibt.
        assert stub.max_concurrent == 1
        assert len(stub.calls) == 3
        assert outcome["results_returned"] == 180
        assert outcome["reported_total"] is None

    async def test_seitenlimit_meldet_abgeschnittenen_lauf(self, abruf):
        stub = _StubConnector(page_size=60, total=1200, max_results=1200)
        execute, runtime = abruf(stub)
        runtime.search_max_pages_per_query = 2
        outcome = await execute(_spec())
        assert outcome["pages"] == 2
        assert outcome["truncated"] is True
        assert outcome["offset_reached"] == 120
        assert outcome["reported_total"] == 1200

    async def test_sortieransicht_steht_im_ergebnis(self, abruf):
        stub = _StubConnector(page_size=60, total=60, max_results=120)
        execute, _runtime = abruf(stub)
        outcome = await execute(_spec(sort="newest"))
        assert outcome["sort"] == "newest"
        assert {call.sort for call in stub.calls} == {"newest"}

    async def test_treffer_werden_gespeichert(self, abruf, db_session):
        from backend.models import Listing

        stub = _StubConnector(page_size=60, total=60, max_results=60)
        execute, _runtime = abruf(stub)
        outcome = await execute(_spec())
        assert outcome["results_usable"] == 60
        assert db_session.query(Listing).count() == 60


class TestVersandsuche:
    """Eine reine Versandsuche gehört in den Abruf, nicht erst in den Filter.

    Kleinanzeigen kann Abholangebote serverseitig ausschließen (/s-versand:ja/).
    Der Auftrag muss deshalb bei jeder Anfrage mitfahren - sonst wären die
    Angebote zwar später ausgeblendet, aber trotzdem abgerufen und gespeichert.
    """

    @pytest.fixture()
    def runner(self, app_state):
        return SearchRunner(
            settings=app_state.settings,
            registry=app_state.registry,
            knowledge=app_state.knowledge,
        )

    @staticmethod
    def _plan() -> FinalSearchPlan:
        return FinalSearchPlan(
            categories=[
                CategoryPlan(
                    category="gpu",
                    label="Grafikkarte",
                    max_price=300.0,
                    queries=[
                        PlannedQuery(
                            term="RTX 3080", family="model", source="model", category="gpu"
                        )
                    ],
                )
            ],
            source="test",
        )

    def test_auftrag_erreicht_jede_anfrage(self, app_state, runner):
        request = SearchRequest(
            product="RTX 3080", platforms=["kleinanzeigen_web"], delivery="shipping"
        )
        specs = runner._materialize_queries(self._plan(), ["kleinanzeigen_web"], request)
        assert specs
        assert all(spec.shipping_only for spec, _ in specs)

    def test_abholsuche_setzt_keinen_versandauftrag(self, app_state, runner):
        request = SearchRequest(
            product="RTX 3080",
            platforms=["kleinanzeigen_web"],
            delivery="pickup",
            postal_code="10115",
        )
        specs = runner._materialize_queries(self._plan(), ["kleinanzeigen_web"], request)
        assert specs
        assert not any(spec.shipping_only for spec, _ in specs)


class TestLimitsUndAnsichten:
    @pytest.fixture()
    def runner(self, app_state):
        return SearchRunner(
            settings=app_state.settings,
            registry=app_state.registry,
            knowledge=app_state.knowledge,
        )

    def test_seitenlimit_richtet_sich_nach_der_plattform(self, app_state, runner):
        runtime = RuntimeSettings()
        ebay = app_state.registry.get("ebay_web")
        caps = ebay.capabilities
        # eBay erlaubt 480 Treffer je Suchansicht; bei 240er-Seiten sind das 2.
        assert runner._page_budget(ebay, caps.page_size_max or 240, runtime) == 2

    def test_ohne_obergrenze_gilt_das_lauf_limit(self, app_state, runner):
        runtime = RuntimeSettings()
        local = app_state.registry.get("local_import")
        assert runner._page_budget(local, 200, runtime) == runtime.search_max_pages_per_query

    def test_kleinanzeigen_reicht_bis_zu_seiner_obergrenze(self, app_state, runner):
        """Die Seitengrenze darf die Obergrenze einer kleinseitigen Quelle nicht abschneiden.

        Kleinanzeigen liefert fest 25 Treffer je Seite. Mit dem früheren
        Standard von 5 Seiten wurden nur 125 der erlaubten 500 Treffer geholt -
        und Kleinanzeigen ist die größte Quelle des Projekts. Zwei Einheiten
        treffen hier aufeinander: Seiten (Einstellung) und Treffer (Quelle).
        """
        runtime = RuntimeSettings()
        ka = app_state.registry.get("kleinanzeigen_web")
        caps = ka.capabilities
        page_size = caps.page_size_max or 25
        pages = runner._page_budget(ka, page_size, runtime)
        assert page_size == 25
        assert caps.max_results_per_query == 500
        assert pages == 20
        assert pages * page_size == caps.max_results_per_query

    def test_grosse_seiten_bleiben_beim_plattformdeckel(self, app_state, runner):
        """Ein groesserer Seitenstandard aendert bei eBay nichts (2 Seiten = 480 Treffer)."""
        runtime = RuntimeSettings()
        ebay = app_state.registry.get("ebay_web")
        caps = ebay.capabilities
        assert runtime.search_max_pages_per_query >= 20
        assert runner._page_budget(ebay, caps.page_size_max or 240, runtime) == 2


class TestPreisgruende:
    """Der Lauf soll benennen, *warum* Angebote am Preis scheitern.

    Ohne diese Aufschlüsselung sieht ein Lauf, der alles am Kauf-Limit
    verwirft, aus wie "hat nichts gefunden".
    """

    @pytest.fixture()
    def runner(self, app_state):
        return SearchRunner(
            settings=app_state.settings,
            registry=app_state.registry,
            knowledge=app_state.knowledge,
        )

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("Über dem Kauf-Limit für NVIDIA GeForce RTX 3060 (165 €): 225.00 €", "über dem Kauf-Limit der Komponente"),
            ("Preis über dem Limit von 100.00 EUR", "über dem Höchstpreis"),
            ("Preis unter dem Mindestpreis von 41.25 EUR", "unter dem Mindestpreis"),
            ("Veröffentlichung liegt außerhalb des gewählten Zeitraums", "außerhalb des gewählten Zeitraums"),
            (None, "Preis nicht angegeben"),
        ],
    )
    def test_grund_wird_erkannt(self, reason, expected):
        assert _price_reason_key(reason) == expected

    def test_zusammenfassung_nennt_preisausschluesse(self, runner):
        control = RunControl(run_id=1)
        runner._merge_counters(
            control,
            {
                "excluded": {"price": 3},
                "excluded_reasons": {"über dem Kauf-Limit der Komponente": 3},
            },
        )
        text = runner._selection_summary(control)
        assert "3 wegen des Preises" in text
        assert "über dem Kauf-Limit der Komponente" in text

    def test_ohne_ausschluesse_bleibt_die_meldung_neutral(self, runner):
        assert (
            runner._selection_summary(RunControl(run_id=1))
            == "Auswahl: keine Angebote aussortiert."
        )

    def test_breite_suche_bekommt_die_zweite_ansicht(self, app_state, runner):
        broad = PlannedQuery(term="grafikkarte", family="generic", source="x", category="gpu")
        narrow = PlannedQuery(term="RTX 3080", family="exact_model", source="x", category="gpu")
        assert runner._views_for("ebay_web", broad) == (None, "newest")
        assert runner._views_for("ebay_web", narrow) == (None,)

    def test_lokaler_import_hat_keine_zweite_ansicht(self, app_state, runner):
        broad = PlannedQuery(term="grafikkarte", family="generic", source="x", category="gpu")
        assert runner._views_for("local_import", broad) == (None,)

    def test_anfragen_werden_je_ansicht_angelegt(self, app_state, runner):
        plan = FinalSearchPlan(
            categories=[
                CategoryPlan(
                    category="gpu",
                    label="Grafikkarte",
                    max_price=300.0,
                    queries=[
                        PlannedQuery(
                            term="grafikkarte", family="generic", source="x", category="gpu"
                        ),
                        PlannedQuery(
                            term="RTX 3080", family="exact_model", source="x", category="gpu"
                        ),
                    ],
                )
            ],
            source="deterministic",
        )
        request = SearchRequest(platforms=["ebay_web"], categories=["gpu"])
        pending = runner._materialize_queries(plan, ["ebay_web"], request)
        seen = {(spec.term, spec.sort) for spec, _variant in pending}
        assert ("grafikkarte", None) in seen
        assert ("grafikkarte", "newest") in seen
        assert ("RTX 3080", None) in seen
        # Die enge Suche braucht keine zweite Ansicht.
        assert ("RTX 3080", "newest") not in seen
        # Die Ansicht gehört zum Schlüssel: sonst gälten beide Anfragen als
        # dieselbe Anfrage.
        assert len({spec.key() for spec, _variant in pending}) == len(pending)

    def test_mindestpreis_geht_in_die_anfrage(self, app_state, runner):
        """Der Mindestpreis ist ein Viertel des wirksamen Höchstpreises - auch bei
        Anfragen, die auf das Kategorie-Budget zurückfallen."""
        plan = FinalSearchPlan(
            categories=[
                CategoryPlan(
                    category="gpu",
                    label="Grafikkarte",
                    max_price=400.0,
                    queries=[
                        PlannedQuery(
                            term="RTX 3080", family="exact_model", source="x", category="gpu"
                        )
                    ],
                )
            ],
            source="deterministic",
        )
        request = SearchRequest(platforms=["local_import"], categories=["gpu"])
        pending = runner._materialize_queries(plan, ["local_import"], request)
        assert pending
        assert {spec.max_price for spec, _variant in pending} == {400.0}
        assert {spec.min_price for spec, _variant in pending} == {100.0}

    def test_eigenes_limit_der_anfrage_bestimmt_den_mindestpreis(self, app_state, runner):
        """Im Preislisten-Modus gilt das Limit der Komponente."""
        plan = FinalSearchPlan(
            categories=[
                CategoryPlan(
                    category="gpu",
                    label="Grafikkarte",
                    max_price=400.0,
                    queries=[
                        PlannedQuery(
                            term="RTX 3080 10GB",
                            family="exact_model",
                            source="price_list",
                            category="gpu",
                            max_price=240.0,
                        )
                    ],
                )
            ],
            source="deterministic",
        )
        request = SearchRequest(price_groups=["gpu"], platforms=["local_import"])
        pending = runner._materialize_queries(plan, ["local_import"], request)
        assert {spec.max_price for spec, _variant in pending} == {240.0}
        assert {spec.min_price for spec, _variant in pending} == {60.0}
