"""Der eBay-Connector gegen einen Mock-eBay-Server.

Geprüft wird der echte HTTP-Pfad: OAuth-Token (client_credentials), Suchanfrage
über die Browse API, Auswertung der Antwort und der Detailabruf. Ein echter
eBay-Aufruf ist in Tests nicht möglich (und auch nicht nötig): Der Mock
antwortet wie die dokumentierte API, sodass Fehler in Anfrageaufbau und
Antwortauswertung auffallen, bevor echte Zugangsdaten im Spiel sind.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.config import Settings
from backend.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorRateLimitError,
    ConnectorUnavailableError,
    LocationFilter,
    SearchQuerySpec,
)
from backend.connectors.ebay import EbayConnector

ITEM = {
    "itemId": "v1|123456789|0",
    "title": "ASUS GeForce RTX 3080 TUF Gaming 10GB",
    "itemWebUrl": "https://www.ebay.de/itm/123456789",
    "price": {"value": "210.00", "currency": "EUR"},
    "shippingOptions": [
        {"shippingCostType": "FIXED", "shippingCost": {"value": "6.99", "currency": "EUR"}}
    ],
    "deliveryOptions": ["SELLER_ARRANGED_LOCAL_PICKUP"],
    "buyingOptions": ["FIXED_PRICE"],
    "condition": "Gebraucht",
    "conditionId": "3000",
    "itemCreationDate": "2026-10-02T09:15:00.000Z",
    "seller": {"username": "techsammler", "feedbackPercentage": "99.4", "feedbackScore": 1200},
    "itemLocation": {"city": "München", "postalCode": "80331", "country": "DE"},
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l500.jpg"},
    "categoryPath": "Computer/Tablets & Netzwerk/Grafikkarten",
    "categoryId": "27386",
    "shortDescription": "Karte laeuft einwandfrei, mit Rechnung.",
}


class FakeEbay:
    """Minimaler eBay-Ersatz mit den drei verwendeten Endpunkten."""

    def __init__(self, port: int = 0) -> None:
        self.port = port
        self.token_requests: list[dict] = []
        self.search_requests: list[dict] = []
        self.detail_requests: list[dict] = []
        self.token_status = 200
        self.search_status = 200
        self.detail_status = 200
        self.token_payload: dict | None = None
        self.search_payload: dict | None = None
        self.detail_payload: dict | None = None
        self.fail_search_once = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- Lebenszyklus ------------------------------------------------------

    def start(self) -> "FakeEbay":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # noqa: D102
                return

            def _send(self, status: int, payload: dict | str) -> None:
                body = (
                    payload.encode("utf-8")
                    if isinstance(payload, str)
                    else json.dumps(payload).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Limit", "5000")
                self.send_header("X-RateLimit-Remaining", "4995")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - HTTP-Verb
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else ""
                if urlparse(self.path).path.endswith("/identity/v1/oauth2/token"):
                    outer.token_requests.append(
                        {"authorization": self.headers.get("Authorization"), "body": body}
                    )
                    if outer.token_status != 200:
                        self._send(outer.token_status, {"error": "invalid_client"})
                        return
                    self._send(
                        200,
                        outer.token_payload
                        or {"access_token": "mock-token", "expires_in": 7200, "token_type": "Bearer"},
                    )
                    return
                self._send(404, {"error": "not found"})

            def do_GET(self) -> None:  # noqa: N802 - HTTP-Verb
                parsed = urlparse(self.path)
                if parsed.path.endswith("/buy/browse/v1/item_summary/search"):
                    outer.search_requests.append(
                        {
                            "params": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                        }
                    )
                    if outer.fail_search_once:
                        outer.fail_search_once = False
                        self._send(400, {"errors": [{"message": "invalid filter"}]})
                        return
                    if outer.search_status != 200:
                        self._send(outer.search_status, {"errors": [{"message": "kaputt"}]})
                        return
                    self._send(
                        200,
                        outer.search_payload
                        or {"total": 1, "offset": 0, "limit": 50, "itemSummaries": [ITEM]},
                    )
                    return
                if "/buy/browse/v1/item/" in parsed.path:
                    item_id = parsed.path.rsplit("/", 1)[-1]
                    outer.detail_requests.append(
                        {
                            "item_id": item_id,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                        }
                    )
                    if outer.detail_status != 200:
                        self._send(outer.detail_status, {"errors": [{"message": "kaputt"}]})
                        return
                    self._send(
                        200,
                        outer.detail_payload
                        or {**ITEM, "description": "<p>Volltext mit <b>HTML</b></p>"},
                    )
                    return
                self._send(404, {"error": "not found"})

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]  # type: ignore[union-attr]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture()
def ebay_server():
    server = FakeEbay().start()
    try:
        yield server
    finally:
        server.stop()


def make_settings(server: FakeEbay | None, **overrides) -> Settings:
    values = {
        "ebay_client_id": "app-id-123",
        "ebay_client_secret": "cert-id-456",
        "ebay_marketplace_id": "EBAY_DE",
        "ebay_api_base": server.base_url if server else "http://127.0.0.1:1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture()
def connector(ebay_server):
    instance = EbayConnector(make_settings(ebay_server))
    return instance


def query(**overrides) -> SearchQuerySpec:
    values = {"term": "rtx 3080", "platform": "ebay", "page_size": 50}
    values.update(overrides)
    return SearchQuerySpec(**values)


class TestOhneZugangsdaten:
    def test_connector_meldet_fehlende_zugangsdaten(self):
        caps = EbayConnector(make_settings(None, ebay_client_id="", ebay_client_secret="")).capabilities
        assert caps.available is False
        assert caps.requires_credentials is True
        assert "EBAY_CLIENT_ID" in (caps.reason_unavailable or "")

    @pytest.mark.asyncio()
    async def test_suche_ohne_zugangsdaten_wird_abgelehnt(self):
        instance = EbayConnector(
            make_settings(None, ebay_client_id="", ebay_client_secret="")
        )
        with pytest.raises(ConnectorUnavailableError):
            await instance.search_page(query())


class TestAuthentifizierung:
    @pytest.mark.asyncio()
    async def test_token_wird_geholt_und_gecacht(self, connector, ebay_server):
        await connector.search_page(query())
        await connector.search_page(query())
        assert len(ebay_server.token_requests) == 1, "Token muss wiederverwendet werden"
        request = ebay_server.token_requests[0]
        expected = base64.b64encode(b"app-id-123:cert-id-456").decode("ascii")
        assert request["authorization"] == f"Basic {expected}"
        assert "grant_type=client_credentials" in request["body"]
        assert "api_scope" in request["body"]

    @pytest.mark.asyncio()
    async def test_abgelehnte_zugangsdaten_ergeben_klare_meldung(self, connector, ebay_server):
        ebay_server.token_status = 401
        with pytest.raises(ConnectorAuthError) as excinfo:
            await connector.search_page(query())
        assert "abgelehnt" in str(excinfo.value)

    @pytest.mark.asyncio()
    async def test_health_check_meldet_erfolg(self, connector):
        ok, reason = await connector.health_check()
        assert ok is True and reason is None

    @pytest.mark.asyncio()
    async def test_health_check_meldet_fehler(self, connector, ebay_server):
        ebay_server.token_status = 401
        ok, reason = await connector.health_check()
        assert ok is False and reason


class TestZustandsfilter:
    """Der Suchlauf übergibt die gebrauchten Zustände als Rangzahlen."""

    def test_rangzahlen_werden_zu_einem_zustand(self, connector):
        value, used = connector._build_filter(
            SearchQuerySpec(
                term="rtx 3080", platform="ebay", condition_texts=["1", "2", "3"]
            )
        )
        # Die API nimmt genau einen Zustand: der weite gebrauchte zuerst.
        assert "conditions:{USED_ACCEPTABLE}" in value
        assert used is True

    def test_ohne_zustaende_kein_zustandsfilter(self, connector):
        value, used = connector._build_filter(SearchQuerySpec(term="rtx 3080", platform="ebay"))
        assert "conditions:" not in value
        assert used is False


class TestSuche:
    @pytest.mark.asyncio()
    async def test_anfrage_enthaelt_die_dokumentierten_parameter(self, connector, ebay_server):
        await connector.search_page(
            query(
                max_price=300.0,
                min_price=100.0,
                condition_texts=[2],
                location=LocationFilter(postal_code="80331", radius_km=25.0),
            )
        )
        sent = ebay_server.search_requests[0]
        params = sent["params"]
        assert params["q"] == "rtx 3080"
        assert params["limit"] == "50"
        assert params["offset"] == "0"
        assert params["fieldgroups"] == "EXTENDED"
        filter_value = params["filter"]
        assert "buyingOptions:{FIXED_PRICE}" in filter_value
        assert "price:[100.00..300.00]" in filter_value
        assert "priceCurrency:EUR" in filter_value
        assert "conditions:{USED_GOOD}" in filter_value
        assert "itemLocationCountry:DE" in filter_value
        assert "pickupPostalCode:80331" in filter_value
        assert "pickupRadius:25" in filter_value
        assert sent["headers"]["x-ebay-c-marketplace-id"] == "EBAY_DE"
        assert "country=DE" in sent["headers"]["x-ebay-c-enduserctx"]
        assert "zip=80331" in sent["headers"]["x-ebay-c-enduserctx"]
        assert sent["headers"]["authorization"] == "Bearer mock-token"

    @pytest.mark.asyncio()
    async def test_treffer_werden_vollstaendig_gemappt(self, connector, ebay_server):
        page = await connector.search_page(query())
        assert page.count == 1
        listing = page.listings[0]
        assert listing.platform == "ebay"
        assert listing.platform_listing_id == "v1|123456789|0"
        assert listing.title == "ASUS GeForce RTX 3080 TUF Gaming 10GB"
        assert listing.url == "https://www.ebay.de/itm/123456789"
        assert listing.price == 210.0
        assert listing.currency == "EUR"
        assert listing.shipping_cost == 6.99
        assert listing.shipping_cost_known is True
        assert listing.pickup_available is True
        assert listing.buying_options == ["FIXED_PRICE"]
        assert listing.condition_text == "Gebraucht"
        assert listing.condition_id == "3000"
        assert listing.seller_name == "techsammler"
        assert listing.seller_feedback_percent == 99.4
        assert listing.seller_feedback_score == 1200
        assert listing.postal_code == "80331"
        assert listing.location_text == "München"
        assert listing.country == "DE"
        assert listing.published_at is not None
        assert listing.image_url.endswith(".jpg")
        assert page.meta["quota"]["remaining"] == "4995"

    @pytest.mark.asyncio()
    async def test_kostenloser_versand_wird_als_null_gewertet(self, connector, ebay_server):
        ebay_server.search_payload = {
            "total": 1,
            "itemSummaries": [
                {**ITEM, "shippingOptions": [{"shippingCostType": "FREE"}]}
            ],
        }
        page = await connector.search_page(query())
        listing = page.listings[0]
        assert listing.shipping_cost == 0.0
        assert listing.shipping_cost_known is True

    @pytest.mark.asyncio()
    async def test_defekter_treffer_bricht_die_seite_nicht_ab(self, connector, ebay_server):
        ebay_server.search_payload = {
            "total": 3,
            "itemSummaries": [ITEM, {"itemId": "x"}, {"title": "ohne URL", "price": None}],
        }
        page = await connector.search_page(query())
        assert page.count == 1, "Nur der vollständige Treffer darf übernommen werden"

    @pytest.mark.asyncio()
    async def test_pagination_ueber_naechsten_offset(self, connector, ebay_server):
        ebay_server.search_payload = {
            "total": 120,
            "offset": 0,
            "limit": 2,
            "next": "https://api.ebay.com/buy/browse/v1/item_summary/search?offset=2",
            "itemSummaries": [
                ITEM,
                {**ITEM, "itemId": "v1|222|0", "itemWebUrl": "https://www.ebay.de/itm/222"},
            ],
        }
        page = await connector.search_page(query(page_size=2))
        assert page.total_reported == 120
        assert page.has_next is True
        assert page.next_offset == 2

    @pytest.mark.asyncio()
    async def test_abgelehnter_zustandsfilter_wird_ohne_ihn_wiederholt(self, connector, ebay_server):
        ebay_server.fail_search_once = True
        page = await connector.search_page(query(condition_texts=[2]))
        assert page.count == 1, "Der zweite Versuch muss Treffer liefern"
        assert len(ebay_server.search_requests) == 2
        assert "conditions:" not in ebay_server.search_requests[1]["params"]["filter"]
        assert any("Zustandsfilter" in item for item in page.limitations)

    @pytest.mark.asyncio()
    async def test_zugriffslimit_wird_eigenstaendig_gemeldet(self, connector, ebay_server):
        ebay_server.search_status = 429
        with pytest.raises(ConnectorRateLimitError):
            await connector.search_page(query())

    @pytest.mark.asyncio()
    async def test_serverfehler_wird_gemeldet(self, connector, ebay_server):
        ebay_server.search_status = 500
        with pytest.raises(ConnectorError):
            await connector.search_page(query())

    @pytest.mark.asyncio()
    async def test_hoechstzahl_wird_als_einschraenkung_ausgewiesen(self, connector, ebay_server):
        ebay_server.search_payload = {
            "total": 25_000,
            "limit": 200,
            "itemSummaries": [ITEM],
        }
        page = await connector.search_page(query(page_size=200))
        assert any("10000" in item.replace(".", "") for item in page.limitations)


class TestDetails:
    @pytest.mark.asyncio()
    async def test_detailabruf_liefert_klartext(self, connector, ebay_server):
        listing = await connector.fetch_details("v1|123456789|0")
        assert listing is not None
        assert listing.description == "Volltext mit HTML"
        assert ebay_server.detail_requests[0]["item_id"] == "v1|123456789|0"

    @pytest.mark.asyncio()
    async def test_unbekanntes_angebot_liefert_none(self, connector, ebay_server):
        ebay_server.detail_status = 404
        assert await connector.fetch_details("v1|unbekannt|0") is None

    @pytest.mark.asyncio()
    async def test_ohne_id_kein_abruf(self, connector, ebay_server):
        assert await connector.fetch_details("") is None
        assert ebay_server.detail_requests == []


class TestNetzfehler:
    @pytest.mark.asyncio()
    async def test_nicht_erreichbarer_server_wird_gemeldet(self):
        instance = EbayConnector(make_settings(None))
        try:
            with pytest.raises(ConnectorError):
                await instance.search_page(query())
        finally:
            await instance.aclose()

    @pytest.mark.asyncio()
    async def test_http_client_kann_uebergeben_werden(self, ebay_server):
        async with httpx.AsyncClient(timeout=10.0) as client:
            instance = EbayConnector(make_settings(ebay_server), client=client)
            page = await instance.search_page(query())
            assert page.count == 1
            await instance.aclose()
            assert client.is_closed is False, "Übergebener Client darf nicht geschlossen werden"
