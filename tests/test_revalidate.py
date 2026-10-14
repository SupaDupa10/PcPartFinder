"""Bestandsprüfung: Der gespeicherte Bestand wird erneut beurteilt.

Der Kern dieser Prüfung ist die Altlast: Angebote, die eine frühere, schwächere
Fassung der Titelregeln durchgelassen hat (Kartons, Lüfter, Netzteile,
Komplett-Rechner, Suchgesuche), müssen aus der Ergebnisliste verschwinden -
ohne Datenverlust und mit nachvollziehbarer Begründung.
"""

from __future__ import annotations

import pytest

from backend.domain.price_limits import PriceLimit, PriceList
from backend.models import (
    STATUS_ACTIVE,
    STATUS_GONE,
    STATUS_REJECTED,
    Listing,
)
from backend.search.revalidate import revalidate_stored_listings


def _price_list(*terms: str) -> PriceList:
    return PriceList(
        entries=[
            PriceLimit(
                group="gpu",
                group_label="Grafikkarten",
                category="gpu",
                name=term,
                term=term,
                max_price=400.0,
            )
            for term in terms
        ]
    )


def _listing(session, title: str, **kwargs) -> Listing:
    listing = Listing(
        platform=kwargs.pop("platform", "ebay_web"),
        platform_listing_id=kwargs.pop("platform_listing_id", title[:24]),
        url=kwargs.pop("url", f"https://example.invalid/{abs(hash(title))}"),
        title=title,
        price=kwargs.pop("price", 200.0),
        total_price=kwargs.pop("total_price", 200.0),
        category=kwargs.pop("category", "gpu"),
        model=kwargs.pop("model", "RTX 3070"),
        status=kwargs.pop("status", STATUS_ACTIVE),
        listing_format="fixed_price",
        **kwargs,
    )
    session.add(listing)
    session.commit()
    return listing


class TestBestandspruefung:
    def test_altlast_wird_aussortiert(self, db_session):
        """Genau diese Titel standen in der Ergebnisliste und gehören dort nicht hin."""
        gpu = _listing(db_session, "GIGABYTE GeForce RTX 3070 GAMING OC 8GB Grafikkarte")
        karton = _listing(db_session, "AMD rx 6800 XT OVP Kartons zwei stück")
        luefter = _listing(db_session, "AMD Sapphire RX 5700(XT) Lüfter (Non RGB)")
        gesuch = _listing(db_session, "Suche RTX 5070 Ti – mit Rechnung")
        laptop = _listing(
            db_session,
            "Gaming Laptop | RTX 5090 | Ultra 9 275HX | 32GB DDR5 | 2TB",
        )

        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3070"))

        assert report.geprueft == 5
        assert report.aussortiert == 4
        assert gpu.status == STATUS_ACTIVE
        for row in (karton, luefter, gesuch, laptop):
            assert row.status == STATUS_REJECTED, row.title
            assert row.reject_reason
            assert any("Bestandsprüfung" in str(item) for item in (row.warnings or []))

    def test_grund_und_art_werden_festgehalten(self, db_session):
        karton = _listing(db_session, "Asus Prime GeForce RTX5060ti 16gb Karton")
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 5060 Ti"))
        assert report.aussortiert == 1
        assert report.beispiele[0]["listing_id"] == karton.id
        assert report.beispiele[0]["art"] == "verpackung"
        assert "Verpackung" in report.summary()

    def test_merkliste_und_lokaler_import_bleiben_unangetastet(self, db_session):
        gemerkt = _listing(db_session, "Karton für RTX 3070", is_favorite=True)
        importiert = _listing(
            db_session, "Karton für RTX 3070", platform="local_import", url="lokal"
        )
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3070"))
        assert report.aussortiert == 0
        assert report.geschont == 2
        assert gemerkt.status == STATUS_ACTIVE and importiert.status == STATUS_ACTIVE

    def test_bereits_aussortiertes_wird_nicht_erneut_geprueft(self, db_session):
        _listing(db_session, "Suche RTX 3070", status=STATUS_REJECTED)
        _listing(db_session, "RTX 3070", status=STATUS_GONE)
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3070"))
        assert report.geprueft == 0

    def test_mehrfache_pruefung_ist_stabil(self, db_session):
        _listing(db_session, "Suche RTX 3070")
        erste = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3070"))
        zweite = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3070"))
        assert erste.aussortiert == 1
        assert zweite.geprueft == 0 and zweite.aussortiert == 0

    def test_ohne_liste_entscheidet_die_schlagwortregel(self, db_session):
        karte = _listing(db_session, "MSI GeForce RTX 3070 Ventus 3X OC")
        karton = _listing(db_session, "Originalverpackung für Palit RTX 3070")
        report = revalidate_stored_listings(db_session, price_list=None)
        assert report.aussortiert == 1
        assert karte.status == STATUS_ACTIVE
        assert karton.status == STATUS_REJECTED


class TestImSuchlauf:
    def test_jeder_lauf_prueft_den_bestand_mit(self, client, app_state):
        """Der Lauf räumt Altlasten auf, bevor er sucht - und meldet es."""
        import time

        session = app_state.session_factory()
        try:
            alt = _listing(session, "Suche RTX 3070 – nur Abholung")
            alt_id = alt.id
        finally:
            session.close()

        created = client.post(
            "/api/search/runs",
            json={
                "categories": ["gpu"],
                "platforms": ["local_import"],
                "max_price": 300,
                "delivery": "both",
                "postal_code": "",
                "days_back": 3650,
            },
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        deadline = time.monotonic() + 30
        run = {}
        while time.monotonic() < deadline:
            run = client.get(f"/api/search/runs/{run_id}").json()
            if run["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)

        revalidation = run["progress"]["revalidation"]
        assert revalidation["aussortiert"] == 1
        assert revalidation["geprueft"] == 1
        assert any(
            "Bestandsprüfung" in str(item)
            for item in run["progress"].get("limitations", [])
        )
        listed = {item["id"] for item in client.get("/api/listings").json()["items"]}
        assert alt_id not in listed


class TestApi:
    def test_endpunkt_prueft_den_bestand(self, client, app_state):
        session = app_state.session_factory()
        try:
            _listing(session, "Suche RTX 3070")
            karte = _listing(
                session, "MSI GeForce RTX 3070 Ventus 3X OC", platform_listing_id="karte-1"
            )
            karte_id = karte.id
        finally:
            session.close()

        response = client.post("/api/listings/revalidate")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["aussortiert"] == 1
        assert "Bestandsprüfung" in payload["summary"]

        # Das aussortierte Angebot ist aus der Liste verschwunden, andere bleiben.
        listed = client.get("/api/listings").json()
        titles = [item["title"] for item in listed["items"]]
        assert "Suche RTX 3070" not in titles
        assert any(item["id"] == karte_id for item in listed["items"])


class TestBeschreibungRettung:
    """Eine über die Beschreibung gerettete Anzeige darf nicht sofort wieder fliegen.

    Die Bestandsprüfung läuft am Anfang jedes Suchlaufs. Würde sie nur den Titel
    ansehen, sortierte sie genau das aus, was der Suchlauf gerade über die
    Beschreibung zugeordnet hat.
    """

    TITEL = "Grafikkarte abzugeben, siehe Beschreibung"

    def test_beschreibung_haelt_die_anzeige_im_bestand(self, db_session):
        anzeige = _listing(
            db_session,
            self.TITEL,
            description="Verkaufe eine MSI RTX 3060 Gaming X, 12GB, wenig genutzt.",
            model="RTX 3060",
        )
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3060"))
        assert anzeige.status == STATUS_ACTIVE
        assert report.aussortiert == 0
        assert report.gerettet == 1
        assert "Beschreibung" in report.summary()
        assert report.to_dict()["gerettet"] == 1

    def test_ohne_passende_beschreibung_wird_weiterhin_aussortiert(self, db_session):
        anzeige = _listing(db_session, self.TITEL, model="RTX 3060")
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3060"))
        assert anzeige.status == STATUS_REJECTED
        assert report.aussortiert == 1
        assert report.gerettet == 0

    def test_teil_titel_bleibt_auch_mit_passender_beschreibung_aussortiert(self, db_session):
        anzeige = _listing(
            db_session,
            "GPU Halterung für RTX 3060, Stütze",
            description="Verkaufe eine MSI RTX 3060 Gaming X, 12GB.",
            model="RTX 3060",
        )
        report = revalidate_stored_listings(db_session, price_list=_price_list("RTX 3060"))
        assert anzeige.status == STATUS_REJECTED
        assert report.aussortiert == 1
        assert report.gerettet == 0
