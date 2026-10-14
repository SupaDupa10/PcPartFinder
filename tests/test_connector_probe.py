"""Der "Testen"-Knopf: prüft Plattformen wirklich, statt nur die Konfiguration.

Bei eBay läuft dafür eine echte Mini-Suche gegen den (im Test gemockten)
eBay-Server. Damit ist belegt, dass der Test nicht bloß "konfiguriert" meldet,
sondern tatsächlich Treffer verarbeitet.
"""

from __future__ import annotations

import json

import pytest

from backend.connectors.ebay import EbayConnector
from backend.connectors.kleinanzeigen import KleinanzeigenConnector
from backend.connectors.local_import import LocalImportConnector
from tests.test_ebay_connector import FakeEbay, make_settings


@pytest.fixture()
def client(app_state):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app()
    app.state.container = app_state
    with TestClient(app) as test_client:
        yield test_client


class TestApiEndpunkt:
    def test_unbekannte_plattform_ergibt_404(self, client):
        response = client.post("/api/connectors/gibt-es-nicht/test")
        assert response.status_code == 404

    def test_lokaler_import_wird_geprueft_und_meldet_anzahl(self, client):
        response = client.post("/api/connectors/local_import/test")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["details"]["angebote"] > 0
        assert payload["details"]["mit_preis"] > 0
        assert "importierbar" in payload["message"]

    def test_kleinanzeigen_meldet_ehrlich_keinen_weg(self, client):
        response = client.post("/api/connectors/kleinanzeigen/test")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert payload["message"]
        assert payload["details"]["alternativen"], "Es muss einen zulässigen Weg nennen"

    def test_ebay_ohne_zugangsdaten_wird_als_fehler_gemeldet(self, client):
        response = client.post("/api/connectors/ebay/test")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert "EBAY_CLIENT_ID" in payload["message"]


class TestEbayFunktionstest:
    @pytest.mark.asyncio()
    async def test_mini_suche_belegt_funktionsfaehigkeit(self):
        server = FakeEbay().start()
        try:
            connector = EbayConnector(make_settings(server))
            ok, message, details = await connector.test_connection()
        finally:
            server.stop()
        assert ok is True and message is None
        assert details["treffer"] == 1
        assert details["marktplatz"] == "EBAY_DE"
        assert "grafikkarte" in details["ergebnis"]
        # Es muss wirklich gesucht worden sein, nicht nur ein Token geholt.
        assert len(server.search_requests) == 1
        assert server.search_requests[0]["params"]["limit"] == "1"

    @pytest.mark.asyncio()
    async def test_abgelehnte_zugangsdaten_werden_erkannt(self):
        server = FakeEbay().start()
        try:
            server.token_status = 401
            connector = EbayConnector(make_settings(server))
            ok, message, details = await connector.test_connection()
        finally:
            server.stop()
        assert ok is False
        assert "abgelehnt" in (message or "")
        assert details == {}

    @pytest.mark.asyncio()
    async def test_ohne_zugangsdaten_kein_aufruf(self):
        connector = EbayConnector(make_settings(None, ebay_client_id="", ebay_client_secret=""))
        ok, message, _details = await connector.test_connection()
        assert ok is False and "EBAY_CLIENT_ID" in (message or "")


class TestLokalerImportFunktionstest:
    @pytest.mark.asyncio()
    async def test_fehlende_datei_wird_gemeldet(self, tmp_path, settings):
        connector = LocalImportConnector(settings, path=str(tmp_path / "fehlt.json"))
        ok, message, details = await connector.test_connection()
        assert ok is False
        assert "nicht gefunden" in (message or "")
        assert details == {}

    @pytest.mark.asyncio()
    async def test_kaputte_datei_wird_gemeldet(self, tmp_path, settings):
        broken = tmp_path / "kaputt.json"
        broken.write_text("{ das ist kein json", encoding="utf-8")
        connector = LocalImportConnector(settings, path=str(broken))
        ok, message, _details = await connector.test_connection()
        assert ok is False
        assert "gelesen" in (message or "").lower() or "json" in (message or "").lower()

    def test_beispieldaten_bestehen_den_test(self, client):
        """Die mitgelieferten Beispieldaten müssen den Test bestehen."""
        import pathlib

        sample = pathlib.Path("samples/beispiel_angebote.json").resolve()
        document = json.loads(sample.read_text(encoding="utf-8"))
        assert len(document["listings"]) == 16
