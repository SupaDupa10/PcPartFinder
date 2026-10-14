"""End-to-End-Tests ueber die HTTP-API.

Verwendet ausschliesslich den lokalen Import-Connector mit Fixture-Daten.
Es wird nie eine echte Plattform abgefragt.
"""

from __future__ import annotations

import time

import pytest


def base_request(**overrides) -> dict:
    """Gleiche Vorgaben wie die Startseite der Oberflaeche."""
    request = {
        "categories": ["gpu"],
        "platforms": ["local_import"],
        "max_price": 300,
        "delivery": "both",
        "postal_code": "",
        "days_back": 3650,
    }
    request.update(overrides)
    return request


def wait_for_run(client, run_id: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/search/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Suchlauf {run_id} wurde nicht rechtzeitig fertig")


@pytest.fixture()
def finished_run(client):
    created = client.post("/api/search/runs", json=base_request())
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    return run_id, wait_for_run(client, run_id)


class TestSystemEndpoints:
    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_system_status_meldet_ehrliche_zahlen(self, client):
        data = client.get("/api/system/status").json()
        assert data["listings"]["total"] == 0
        assert data["knowledge"]["entries"] > 0
        assert data["knowledge"]["priced_entries"] == 0
        assert data["knowledge"]["identity_only_entries"] == data["knowledge"]["entries"]

    def test_kategorien_sind_vollstaendig(self, client):
        data = client.get("/api/system/categories").json()
        keys = {item["key"] for item in data}
        assert {
            "gpu",
            "cpu",
            "ram",
            "mainboard",
            "ssd",
            "hdd",
            "psu",
            "cooler",
            "case",
            "complete_pc",
            "other",
        } <= keys

    def test_connectoren_zeigen_limits_offen(self, client):
        data = client.get("/api/connectors").json()
        by_platform = {item["platform"]: item for item in data}
        assert set(by_platform) >= {"ebay", "kleinanzeigen", "local_import"}
        # Ohne Zugangsdaten ist eBay nicht nutzbar - das muss sichtbar sein.
        assert by_platform["ebay"]["available"] is False
        assert by_platform["ebay"]["setup_hint"]
        # Kleinanzeigen ist deaktiviert und dokumentiert die Einschraenkung.
        assert by_platform["kleinanzeigen"]["available"] is False
        assert by_platform["local_import"]["available"] is True
        assert by_platform["local_import"]["limitations"]

class TestSearchRun:
    def test_kompletter_lauf_ueber_den_import_connector(self, client, finished_run):
        run_id, run = finished_run
        assert run["status"] == "completed"
        progress = run["progress"]
        assert progress["queries_total"] > 0
        assert progress["queries_done"] > 0
        assert progress["results_usable"] > 0
        assert progress["rounds"] >= 1

    def test_auktionen_werden_ausgeschlossen(self, client, finished_run):
        _, run = finished_run
        counters = run["progress"]["excluded"]
        assert counters["auction"] >= 1
        assert counters["unknown_format"] >= 1

    def test_ergebnisse_haben_links_zum_original(self, client, finished_run):
        listings = client.get("/api/listings", params={"limit": 50}).json()
        assert listings["items"]
        for item in listings["items"]:
            assert item["url"].startswith("http")
            assert item["listing_format"] == "fixed_price"

    def test_angebote_werden_bewertet_und_sortierbar(self, client, finished_run):
        best = client.get("/api/listings", params={"sort": "best_deal", "limit": 50}).json()
        assert best["sort"] == "best_deal"
        scores = [item["deal_score"] for item in best["items"] if item["deal_score"] is not None]
        assert scores == sorted(scores, reverse=True)
        assert all(0 <= score <= 100 for score in scores)

        cheapest = client.get("/api/listings", params={"sort": "lowest_price"}).json()
        prices = [item["total_price"] for item in cheapest["items"] if item["total_price"]]
        assert prices == sorted(prices)

    def test_der_beste_deal_ist_nicht_einfach_der_billigste(self, client, finished_run):
        """Ein defektes Billigangebot darf nicht oben stehen."""
        items = client.get("/api/listings", params={"sort": "best_deal"}).json()["items"]
        top = items[0]
        assert top["condition_rank"] != 0 or top["verdict"] == "risky"
        defective = [item for item in items if item["condition_rank"] == 0]
        if defective:
            assert defective[0]["verdict"] in {"risky", "fair", "no_deal", "insufficient_data"}

    def test_marktwert_und_unsicherheit_sind_transparent(self, client, finished_run):
        detail_id = client.get("/api/listings", params={"limit": 1}).json()["items"][0]["id"]
        detail = client.get(f"/api/listings/{detail_id}").json()
        assert "market_basis" in detail
        assert detail["score_breakdown"] is not None
        assert detail["score_breakdown"]["components"]
        assert detail["reasons"]

    def test_suchabdeckung_wird_ehrlich_gemeldet(self, client, finished_run):
        run_id, _ = finished_run
        coverage = client.get(f"/api/search/runs/{run_id}/coverage").json()
        assert coverage["totals"]["queries"] > 0
        assert coverage["totals"]["successful"] > 0
        # "Vollstaendig durchsucht" wird nie behauptet, solange es nicht belegbar ist.
        assert coverage["fully_searched"] is False
        assert coverage["fully_searched_reason"]
        assert coverage["platforms"]
        assert coverage["notes"]
        assert "not_retrievable" in coverage

    def test_suchanfragen_werden_protokolliert(self, client, finished_run):
        run_id, _ = finished_run
        queries = client.get(f"/api/search/runs/{run_id}/queries").json()
        assert queries and queries[0]["term"]
        assert queries[0]["platform"] == "local_import"
        assert queries[0]["status"]

    def test_lauf_ist_in_der_historie(self, client, finished_run):
        runs = client.get("/api/search/runs").json()
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"


class TestFiltersAndViews:
    def test_filter_nach_kategorie_und_preis(self, client, finished_run):
        filtered = client.get(
            "/api/listings", params={"category": "gpu", "max_price": 250, "limit": 50}
        ).json()
        assert filtered["items"]
        for item in filtered["items"]:
            assert item["category"] == "gpu"
            assert item["total_price"] <= 250

    def test_filter_nur_verifizierte_festpreise(self, client, finished_run):
        data = client.get("/api/listings", params={"fixed_price_only": True, "limit": 50}).json()
        assert all(item["listing_format"] == "fixed_price" for item in data["items"])

    def test_filter_nach_plattform(self, client, finished_run):
        data = client.get("/api/listings", params={"platform": "ebay", "limit": 50}).json()
        assert data["items"] == []
        imported = client.get("/api/listings", params={"platform": "local_import"}).json()
        assert imported["items"]

    def test_filter_nach_mindestscore_und_zustand(self, client, finished_run):
        data = client.get(
            "/api/listings", params={"min_score": 50, "min_condition_rank": 1}
        ).json()
        for item in data["items"]:
            assert item["deal_score"] >= 50
            assert item["condition_rank"] >= 1

    def test_sortierung_nach_aktualitaet(self, client, finished_run):
        data = client.get("/api/listings", params={"sort": "newest"}).json()
        assert data["sort"] == "newest"
        assert data["items"]

    def test_alle_sortierungen_sind_gueltig(self, client, finished_run):
        for sort in (
            "best_deal",
            "price_advantage",
            "lowest_price",
            "price_performance",
            "newest",
            "confidence",
        ):
            response = client.get("/api/listings", params={"sort": sort})
            assert response.status_code == 200, sort
            assert response.json()["sort"] == sort

    def test_ungueltige_sortierung_wird_abgelehnt(self, client, finished_run):
        assert client.get("/api/listings", params={"sort": "quatsch"}).status_code == 422

    def test_leere_ergebnisliste_ist_kein_fehler(self, client):
        data = client.get("/api/listings").json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0

    def test_unbekanntes_angebot_liefert_404(self, client):
        assert client.get("/api/listings/99999").status_code == 404

    def test_filteroptionen_kommen_aus_den_daten(self, client, finished_run):
        data = client.get("/api/listings/filters").json()
        assert {item["value"] for item in data["platforms"]} == {"local_import"}
        assert data["models"]
        assert data["conditions"]
        assert data["knowledge_categories"]

    def test_favoriten_umschalten(self, client, finished_run):
        listing_id = client.get("/api/listings", params={"limit": 1}).json()["items"][0]["id"]
        first = client.post(f"/api/listings/{listing_id}/favorite", params={"favorite": True})
        assert first.status_code == 200 and first.json()["is_favorite"] is True
        favorites = client.get("/api/listings", params={"favorites": True}).json()
        assert [item["id"] for item in favorites["items"]] == [listing_id]
        client.post(f"/api/listings/{listing_id}/favorite", params={"favorite": False})
        assert client.get("/api/listings", params={"favorites": True}).json()["total"] == 0

    def test_erklaerung_bleibt_deterministisch(self, client, finished_run):
        """Die Erklaerung kommt aus den nachvollziehbaren Zahlen, nie aus KI."""
        listing_id = client.get("/api/listings", params={"limit": 1}).json()["items"][0]["id"]
        data = client.post(f"/api/listings/{listing_id}/explain").json()
        assert data["available"] is False
        assert data["source"] == "deterministic"
        assert data["message"]
        assert data["summary"]
        assert data["main_arguments"]

    def test_preisverlauf_ist_abrufbar(self, client, finished_run):
        listing_id = client.get("/api/listings", params={"limit": 1}).json()["items"][0]["id"]
        detail = client.get(f"/api/listings/{listing_id}").json()
        assert detail["price_history"]


def price_list_request(**overrides) -> dict:
    """Suchlauf nach der Kauf-Limit-Liste (wie die Oberfläche ihn startet)."""
    request = {
        "price_groups": ["gpu"],
        "platforms": ["local_import"],
        "days_back": 3650,
    }
    request.update(overrides)
    return request


def use_price_list(app_state, tmp_path, text: str, monkeypatch) -> str:
    """Hängt einen Suchlauf an eine eigene Kauf-Limit-Datei."""
    path = tmp_path / "hoechstpreise.txt"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(app_state.settings, "price_list_path", str(path))
    return str(path)


GPU_LIMITS = """
----------------------------------------------------------------
GRAFIKKARTEN - NVIDIA
----------------------------------------------------------------
NVIDIA GeForce RTX 3080 10GB = 240 €
AMD Radeon RX 6800 XT = 245 €
"""


class TestPreisliste:
    def test_endpunkt_zeigt_gruppen_und_limits(self, client, app_state, tmp_path, monkeypatch):
        path = use_price_list(app_state, tmp_path, GPU_LIMITS, monkeypatch)
        data = client.get("/api/price-limits").json()
        assert data["available"] is True
        assert data["path"] == path
        assert data["loaded_at"]
        assert data["errors"] == []
        assert [group["key"] for group in data["groups"]] == ["gpu"]
        assert data["groups"][0]["count"] == 2
        terms = {entry["term"]: entry["max_price"] for entry in data["entries"]}
        assert terms == {"RTX 3080 10GB": 240.0, "RX 6800 XT": 245.0}

    def test_ohne_liste_ist_die_suche_nicht_moeglich(self, client):
        """Die Tests laufen ohne Liste - der Endpunkt sagt das offen."""
        data = client.get("/api/price-limits").json()
        assert data["available"] is False
        assert data["groups"] == []

    def test_suchlauf_hat_je_komponente_ein_eigenes_limit(
        self, client, app_state, tmp_path, monkeypatch
    ):
        use_price_list(app_state, tmp_path, GPU_LIMITS, monkeypatch)
        created = client.post("/api/search/runs", json=price_list_request())
        assert created.status_code == 201, created.text
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"

        queries = [q for category in run["plan"]["categories"] for q in category["queries"]]
        assert {q["term"]: q["max_price"] for q in queries} == {
            "RTX 3080 10GB": 240.0,
            "RX 6800 XT": 245.0,
        }
        # Der Mindestpreis ist ein Viertel des jeweiligen Limits.
        assert {q["term"]: q["min_price"] for q in queries} == {
            "RTX 3080 10GB": 60.0,
            "RX 6800 XT": 61.25,
        }
        assert run["progress"]["price_list"]["targets"] == 2

        # Gesucht wird wirklich jede Komponente der Liste (protokollierte Anfragen).
        executed = client.get(f"/api/search/runs/{run['id']}/queries").json()
        assert {row["term"] for row in executed} == {"RTX 3080 10GB", "RX 6800 XT"}
        assert run["result_count"] > 0

    def test_angebot_ueber_dem_limit_kommt_nicht_in_die_liste(
        self, client, app_state, tmp_path, monkeypatch
    ):
        """Limit inkl. Versand: 190 € Artikelpreis, 198 € gesamt - zu viel."""
        use_price_list(
            app_state,
            tmp_path,
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "AMD Radeon RX 6800 XT = 195 €\n",
            monkeypatch,
        )
        created = client.post("/api/search/runs", json=price_list_request())
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"
        assert run["result_count"] == 0
        assert run["progress"]["excluded"]["price"] >= 1
        assert client.get("/api/listings").json()["items"] == []

    def test_angebot_unter_dem_mindestpreis_kommt_nicht_in_die_liste(
        self, client, app_state, tmp_path, monkeypatch
    ):
        """Mindestpreis: ein Viertel des Limits - hier 75 €.

        Das defekte 60-€-Angebot liegt unter dem Limit, ist aber so günstig,
        dass es kein Treffer zum Produkt ist. Es darf nirgends auftauchen.
        """
        use_price_list(
            app_state,
            tmp_path,
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 = 300 €\n",
            monkeypatch,
        )
        created = client.post("/api/search/runs", json=price_list_request())
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"

        queries = [q for category in run["plan"]["categories"] for q in category["queries"]]
        assert {q["min_price"] for q in queries} == {75.0}

        items = client.get("/api/listings", params={"limit": 100}).json()["items"]
        assert items, "Die Angebote im Preisbereich müssen bleiben"
        assert not any("Ersatzteil" in item["title"] for item in items)
        assert all((item["total_price"] or item["price"] or 0) >= 75 for item in items)

    def test_liste_verbirgt_teure_angebote_aus_frueheren_laeufen(
        self, client, app_state, tmp_path, monkeypatch, finished_run
    ):
        """Auch alte Angebote über dem Limit tauchen nicht mehr auf."""
        before = client.get("/api/listings", params={"limit": 100}).json()
        assert any(item["total_price"] > 200 for item in before["items"])

        use_price_list(
            app_state,
            tmp_path,
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 10GB = 200 €\n",
            monkeypatch,
        )
        data = client.get("/api/listings", params={"limit": 100}).json()
        assert data["items"]
        teuer = [
            item
            for item in data["items"]
            if "3080" in item["title"] and (item["total_price"] or 0) > 200
        ]
        assert teuer == []
        # Die Gesamtzahl beschreibt genau das, was auch angezeigt wird.
        assert data["total"] == len(data["items"])


class TestFailureIsolation:
    def test_nicht_nutzbare_plattform_erklaert_sich(self, client):
        """Ohne eBay-Zugangsdaten wird der Start abgelehnt - mit Begruendung."""
        response = client.post("/api/search/runs", json=base_request(platforms=["ebay"]))
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "Keine der ausgewählten Plattformen ist nutzbar" in detail
        assert "eBay" in detail
        assert "Abdeckung" in detail

    def test_lauf_startet_ohne_nutzbare_plattform_kontrolliert(self, client):
        created = client.post("/api/search/runs", json=base_request(platforms=["local_import"]))
        assert created.status_code == 201
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"

    def test_leere_suchergebnisse_ohne_treffer(self, client):
        """Ein Suchbegriff ohne Treffer darf keinen Fehler erzeugen."""
        created = client.post(
            "/api/search/runs",
            json=base_request(categories=["other"], max_price=5, price_per_category={}),
        )
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"
        assert run["progress"]["queries_done"] > 0
        assert run["progress"]["results_usable"] == 0

    def test_pagination_ueber_mehrere_seiten(self, client, finished_run):
        """Die Fixture enthaelt mehr Treffer als eine Seite fasst."""
        first = client.get("/api/listings", params={"limit": 3, "offset": 0}).json()
        second = client.get("/api/listings", params={"limit": 3, "offset": 3}).json()
        assert first["total"] == second["total"] > 3
        ids = {item["id"] for item in first["items"]}
        assert not ids & {item["id"] for item in second["items"]}

    def test_keine_doppelten_angebote_in_den_ergebnissen(self, client, finished_run):
        data = client.get("/api/listings", params={"limit": 200}).json()
        urls = [item["url"] for item in data["items"]]
        assert len(urls) == len(set(urls))
        keys = [(item["platform"], item["canonical_url"] if "canonical_url" in item else item["url"]) for item in data["items"]]
        assert len(keys) == len(set(keys))

    def test_doppelte_anzeige_aus_der_quelle_wird_einmal_gespeichert(
        self, client, app_state
    ):
        """Das Fixture enthaelt ein absichtliches Duplikat mit anderer URL."""
        from sqlalchemy import select

        from backend.models import Listing

        with app_state.session_factory() as session:
            created = client.post("/api/search/runs", json=base_request())
            wait_for_run(client, created.json()["run_id"])
            urls = [row.url for row in session.scalars(select(Listing)).all()]
        assert len(urls) == len(set(urls))
        assert any("gpu-good-1" in url for url in urls)

    def test_abbrechen_ist_moeglich(self, client, finished_run):
        """Ein bereits beendeter Lauf laesst sich nicht mehr abbrechen."""
        run_id, _ = finished_run
        response = client.post(f"/api/search/runs/{run_id}/cancel")
        assert response.status_code in {200, 400, 404, 409}

    def test_pausieren_ist_moeglich(self, client, finished_run):
        run_id, _ = finished_run
        response = client.post(f"/api/search/runs/{run_id}/pause")
        assert response.status_code in {200, 400, 404, 409}


class TestProduktsuche:
    """Nur ein Produktname und ein Budget - die Kategorie leitet das Backend ab."""

    def _start(self, client, **overrides):
        payload = base_request(categories=[], product="RTX 3080", **overrides)
        created = client.post("/api/search/runs", json=payload)
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]
        return run_id, wait_for_run(client, run_id)

    def test_lauf_ohne_kategorie_ist_moeglich(self, client):
        run_id, run = self._start(client)
        assert run["status"] == "completed"
        assert run["request"]["product"] == "RTX 3080"
        assert run["result_count"] > 0

    def test_kategorie_wird_aus_dem_namen_abgeleitet(self, client):
        _, run = self._start(client)
        assert run["plan"]["categories"][0]["category"] == "gpu"
        assert run["plan"]["categories"][0]["queries"][0]["term"] == "RTX 3080"
        assert run["plan"]["categories"][0]["max_price"] == 300

    def test_suchanfragen_sind_produktbezogen(self, client):
        run_id, _ = self._start(client)
        queries = client.get(f"/api/search/runs/{run_id}/queries").json()
        terms = [q["term"] for q in queries]
        assert terms[0] == "RTX 3080"
        # Ein Sicherheitsnetz bleibt, es dominiert aber nicht.
        generic = [q for q in queries if q["family"] == "generic"]
        assert len(generic) <= 3

    def test_fremde_produkte_landen_nicht_in_der_liste(self, client):
        self._start(client)
        items = client.get("/api/listings", params={"limit": 500}).json()["items"]
        assert items
        assert all("RTX 3080" in item["title"] or "rtx3080" in item["title"].lower() for item in items)
        assert not any("4090" in item["title"] for item in items)

    def test_budget_grenze_wirkt_weiter(self, client):
        _, run = self._start(client, max_price=250)
        items = client.get("/api/listings", params={"limit": 500}).json()["items"]
        assert run["result_count"] > 0
        assert all((item["total_price"] or item["price"] or 0) <= 250 for item in items)

    def test_reine_abholung_bleibt_beim_standort(self, client):
        """Mit PLZ und Umkreis darf nichts weit entferntes übrig bleiben."""
        self._start(client, delivery="pickup", postal_code="10115", radius_km=50, max_price=800)
        items = client.get("/api/listings", params={"limit": 500}).json()["items"]
        assert items, "Das Angebot in Berlin muss überleben"
        assert {item["postal_code"] for item in items} == {"10115"}

    def test_nur_versand_filtert_abholangebote(self, client):
        self._start(client, delivery="shipping")
        items = client.get("/api/listings", params={"limit": 500}).json()["items"]
        assert items
        assert not any(
            item["pickup_available"] and not item["shipping_cost_known"] for item in items
        )

    def test_unbekanntes_produkt_laeuft_ehrlich_weiter(self, client):
        """Kein Hardwarebezug: breite Suche, klarer Hinweis, kein Absturz."""
        payload = base_request(
            categories=[], product="Schallplattenspieler Thorens TD 160", platforms=["local_import"]
        )
        created = client.post("/api/search/runs", json=payload)
        assert created.status_code == 201
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"
        assert run["plan"]["categories"][0]["queries"][0]["term"] == (
            "Schallplattenspieler Thorens TD 160"
        )
        messages = [
            item.get("message", "") for item in (run.get("limitations") or [])
            if isinstance(item, dict)
        ]
        assert any("keine bekannte Hardware" in message for message in messages)

    def test_fremdes_produkt_erzeugt_keine_treffer_aber_keinen_fehler(self, client):
        payload = base_request(
            categories=[], product="Schallplattenspieler Thorens TD 160", platforms=["local_import"]
        )
        created = client.post("/api/search/runs", json=payload)
        run_id = created.json()["run_id"]
        wait_for_run(client, run_id)
        listings = client.get("/api/listings", params={"limit": 500}).json()
        assert listings["total"] == 0

    def test_mit_standort_und_versand_bleibt_die_quelle_offen(self, client):
        """Bei 'Versand und Abholung' darf der Standort nicht hart filtern."""
        self._start(client, delivery="both", postal_code="10115", radius_km=50, max_price=800)
        items = client.get("/api/listings", params={"limit": 500}).json()["items"]
        assert items
        assert len({item["postal_code"] for item in items}) > 1


class TestValidation:
    def test_ungueltige_kategorie_wird_abgelehnt(self, client):
        response = client.post("/api/search/runs", json=base_request(categories=["quatsch"]))
        assert response.status_code == 422

    def test_leere_kategorieauswahl_wird_abgelehnt(self, client):
        response = client.post("/api/search/runs", json=base_request(categories=[]))
        assert response.status_code == 422

    def test_ungueltiges_delivery_wird_abgelehnt(self, client):
        response = client.post("/api/search/runs", json=base_request(delivery="beamen"))
        assert response.status_code == 422

    def test_abholung_verlangt_plz(self, client):
        response = client.post(
            "/api/search/runs",
            json=base_request(delivery="pickup", postal_code=None),
        )
        assert response.status_code == 422

    def test_produktname_allein_reicht(self, client):
        response = client.post(
            "/api/search/runs",
            json=base_request(categories=[], product="Ryzen 5 5600X"),
        )
        assert response.status_code == 201

    def test_leerer_produktname_wird_ignoriert(self, client):
        """Ein Feld mit Leerzeichen zählt nicht als Produktname."""
        response = client.post(
            "/api/search/runs", json=base_request(categories=[], product="   ")
        )
        assert response.status_code == 422

    def test_ohne_produkt_und_ohne_kategorie_wird_abgelehnt(self, client):
        response = client.post("/api/search/runs", json=base_request(categories=[], product=None))
        assert response.status_code == 422

    def test_standardvorgabe_der_oberflaeche_ist_gueltig(self, client):
        """"Versand und Abholung" darf ohne PLZ startbar sein."""
        response = client.post(
            "/api/search/runs", json=base_request(delivery="both", postal_code="")
        )
        assert response.status_code == 201

    def test_unbekannte_plattform_wird_abgelehnt(self, client):
        response = client.post(
            "/api/search/runs", json=base_request(platforms=["gibt_es_nicht"])
        )
        assert response.status_code == 422

    def test_negativer_preis_wird_abgelehnt(self, client):
        response = client.post("/api/search/runs", json=base_request(max_price=-5))
        assert response.status_code == 422


class TestSettings:
    def test_einstellungen_lesen_und_schreiben(self, client):
        body = client.get("/api/settings").json()
        runtime = body["runtime"]
        assert runtime["weights"]["price_advantage"] > 0

        updated = dict(runtime)
        updated["default_days_back"] = 30
        response = client.put("/api/settings", json=updated)
        assert response.status_code == 200
        assert response.json()["runtime"]["default_days_back"] == 30

    def test_zugangsdaten_werden_nicht_ausgeliefert(self, client):
        body = client.get("/api/settings").json()
        assert body["environment"]["ebay_client_secret"] in {"", "***gesetzt***"}

    def test_gewichte_werden_validiert(self, client):
        weights = client.get("/api/settings").json()["runtime"]["weights"]
        weights["price_advantage"] = 55
        response = client.put("/api/settings/weights", json=weights)
        assert response.status_code == 200
        assert response.json()["price_advantage"] == 55

    def test_ungueltiges_gewicht_wird_abgelehnt(self, client):
        weights = client.get("/api/settings").json()["runtime"]["weights"]
        response = client.put("/api/settings/weights", json={**weights, "price_advantage": -1})
        assert response.status_code == 422

    def test_zuruecksetzen(self, client):
        assert client.post("/api/settings/reset").status_code == 200

    def test_wissensbasis_neu_laden(self, client):
        data = client.post("/api/knowledge/reload").json()
        assert data["entries"] > 0
        assert data["errors"] == []


class TestDatenLeeren:
    """"Alle Angebote und den Suchverlauf loeschen" (Neuanfang)."""

    def test_leert_angebote_und_verlauf(self, client, finished_run):
        vorher = client.get("/api/stats/overview").json()["listings"]["total"]
        assert vorher > 0
        assert client.get("/api/search/runs").json() != []

        response = client.post("/api/maintenance/clear")
        assert response.status_code == 200
        body = response.json()
        assert body["cleared"]["listings"] == vorher
        assert body["cleared"]["search_runs"] >= 1
        assert "Angebote" in body["message"]

        # Beides steht danach bei null - genau das, was die Oberflaeche zeigt.
        assert client.get("/api/listings", params={"limit": 500}).json()["total"] == 0
        assert client.get("/api/stats/overview").json()["listings"]["total"] == 0
        assert client.get("/api/search/runs").json() == []

    def test_einstellungen_und_wissensbasis_bleiben(self, client, finished_run):
        vorher = client.get("/api/settings").json()["runtime"]
        wissen = client.get("/api/system/status").json()["knowledge"]["entries"]
        assert client.post("/api/maintenance/clear").status_code == 200
        nachher = client.get("/api/settings").json()["runtime"]
        assert nachher == vorher
        assert client.get("/api/system/status").json()["knowledge"]["entries"] == wissen

    def test_zweimal_leeren_ist_fehlerfrei(self, client, finished_run):
        assert client.post("/api/maintenance/clear").status_code == 200
        second = client.post("/api/maintenance/clear")
        assert second.status_code == 200
        assert second.json()["cleared"]["listings"] == 0
        assert second.json()["cancelled_runs"] == []

    def test_neue_suche_fuellt_die_liste_wieder(self, client, finished_run):
        """Nach dem Leeren ist die Ergebnisliste leer und ein Lauf fuellt sie neu."""
        assert client.post("/api/maintenance/clear").status_code == 200
        created = client.post("/api/search/runs", json=base_request())
        assert created.status_code == 201
        run = wait_for_run(client, created.json()["run_id"])
        assert run["status"] == "completed"
        assert client.get("/api/listings", params={"limit": 500}).json()["total"] > 0


class TestLieferungsfilterDerListe:
    """Die Lieferungsauswahl der Oberfläche gilt auch für die Ergebnisliste.

    "Nur Versand" muss Angebote ausblenden, die belegt nur abgeholt werden
    können - sonst stünden genau die darin, die man nicht bestellen kann.
    """

    @staticmethod
    def _seed(app_state, key: str, **overrides) -> int:
        from backend.models import Listing

        values = {
            "platform": "kleinanzeigen_web",
            "url": f"https://www.kleinanzeigen.de/s-anzeige/pruefung-{key}/1-1-1",
            "title": f"Prüfangebot {key}",
            "description": None,
            "price": 100.0,
            "total_price": 100.0,
            "shipping_cost_known": False,
            "pickup_available": False,
            "postal_code": "10115",
            "listing_format": "fixed_price",
            "status": "active",
            "deal_score": 80.0,
            "raw": None,
        }
        values.update(overrides)
        session = app_state.session_factory()
        try:
            row = Listing(**values)
            session.add(row)
            session.commit()
            return int(row.id)
        finally:
            session.close()

    @staticmethod
    def _titles(client, **params) -> set[str]:
        response = client.get("/api/listings", params={"limit": 500, **params})
        assert response.status_code == 200
        return {item["title"] for item in response.json()["items"]}

    def _faelle(self, app_state) -> dict[str, int]:
        """Ein Angebot je Belegquelle - und zwei, die sichtbar bleiben müssen."""
        return {
            # Belegt über die Versanddaten: Abholung gemeldet, Versand unbekannt.
            "abholung_daten": self._seed(app_state, "daten", pickup_available=True),
            # Belegt über die Einordnung der Plattform ("Versand möglich" fehlt).
            "abholung_einordnung": self._seed(
                app_state,
                "einordnung",
                raw={"extra": {"source": "kleinanzeigen_web", "shipping_possible": False}},
            ),
            # Belegt über den Anzeigentext.
            "abholung_text": self._seed(app_state, "text", title="RTX 3080 nur Abholung in Berlin"),
            # Versand bekannt: bleibt sichtbar.
            "versand_bekannt": self._seed(
                app_state, "versand", pickup_available=True, shipping_cost_known=True
            ),
            # Versandkosten unbekannt, aber kein Abholbeleg: bleibt sichtbar.
            "versand_unbekannt": self._seed(app_state, "unbekannt"),
            # "Kein Versand ins Ausland" ist kein Abholangebot.
            "versand_ausland": self._seed(
                app_state, "ausland", title="RTX 3080 Versandwelt, kein Versand ins Ausland"
            ),
        }

    def test_nur_versand_blendet_belegte_abholangebote_aus(self, client, app_state):
        self._faelle(app_state)
        sichtbar = self._titles(client, delivery="shipping")
        assert "Prüfangebot daten" not in sichtbar
        assert "Prüfangebot einordnung" not in sichtbar
        assert "RTX 3080 nur Abholung in Berlin" not in sichtbar
        assert {
            "Prüfangebot versand",
            "Prüfangebot unbekannt",
            "RTX 3080 Versandwelt, kein Versand ins Ausland",
        } <= sichtbar

    def test_ohne_lieferungsauswahl_bleibt_alles_sichtbar(self, client, app_state):
        self._faelle(app_state)
        sichtbar = self._titles(client)
        assert "Prüfangebot daten" in sichtbar
        assert "Prüfangebot einordnung" in sichtbar
        assert "RTX 3080 nur Abholung in Berlin" in sichtbar

    def test_nur_versand_zaehlt_die_gefilterten_angebote(self, client, app_state):
        """Blättern darf nicht lügen: ``total`` zählt nur die angezeigten."""
        self._faelle(app_state)
        alle = client.get("/api/listings", params={"limit": 500}).json()
        gefiltert = client.get(
            "/api/listings", params={"limit": 500, "delivery": "shipping"}
        ).json()
        assert gefiltert["total"] == len(gefiltert["items"])
        assert gefiltert["total"] == alle["total"] - 3

    def test_nur_abholung_verlangt_einen_standort(self, client, app_state):
        self._seed(app_state, "ohne-ort", postal_code=None)
        self._seed(app_state, "mit-ort")
        sichtbar = self._titles(client, delivery="pickup")
        assert sichtbar == {"Prüfangebot mit-ort"}

    def test_unbekannte_lieferungsart_wird_abgelehnt(self, client, app_state):
        response = client.get("/api/listings", params={"delivery": "beamen"})
        assert response.status_code == 422
