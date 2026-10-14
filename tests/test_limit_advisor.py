"""Tests für den Limit-Berater und das Zurückschreiben der Preisliste.

Der Berater leitet Kauf-Limits aus den bereits beobachteten Angebotspreisen ab.
Getestet wird genau das, was die Anwendung verspricht: aus dem günstigsten
Zehntel, nur bei ausreichender Datenbasis, in beide Richtungen begrenzt - und
nur die Zahlen der Datei werden ersetzt, nie die Kommentare.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.domain.price_limits import (
    DEFAULT_PRICE_LIST_FILE,
    load_price_list,
    parse_price_limits,
    rewrite_price_limits,
)
from backend.models import (
    FORMAT_FIXED_PRICE,
    FORMAT_UNKNOWN,
    STATUS_ACTIVE,
    STATUS_GONE,
    Listing,
)
from backend.pricing.limit_advisor import (
    DIRECTION_KEEP,
    DIRECTION_LOWER,
    DIRECTION_NO_DATA,
    DIRECTION_RAISE,
    advise_limits,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

SAMPLE = """\
----------------------------------------------------------------
PC-KOMPONENTEN - GEBRAUCHTMARKT-KAUFLIMITS (Stand: 14.10.2026)
----------------------------------------------------------------
Ein Hinweistext ohne Gleichheitszeichen.

----------------------------------------------------------------
GRAFIKKARTEN - NVIDIA
----------------------------------------------------------------
NVIDIA GeForce RTX 3080 10GB = 100 €

----------------------------------------------------------------
HINWEISE
----------------------------------------------------------------
Faustregel fuer Nachverhandlungen = 15 % unter dem Limit.
"""


@pytest.fixture()
def list_path(tmp_path) -> Path:
    path = tmp_path / "hoechstpreise.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def add_listing(
    session,
    *,
    title: str = "ASUS RTX 3080 10GB OC",
    model: str = "RTX 3080",
    category: str = "gpu",
    total: float | None = 90.0,
    condition_rank: int | None = 2,
    age_days: int = 5,
    listing_format: str = FORMAT_FIXED_PRICE,
    status: str = STATUS_ACTIVE,
) -> Listing:
    observed = NOW - timedelta(days=age_days)
    listing = Listing(
        platform="test",
        platform_listing_id=None,
        url=f"https://example.invalid/{model}/{total}/{age_days}/{id(object())}",
        title=title,
        price=total,
        shipping_cost_known=True,
        total_price=total,
        category=category,
        model=model,
        condition_rank=condition_rank,
        listing_format=listing_format,
        status=status,
        fetched_at=observed,
        last_seen_at=observed,
    )
    session.add(listing)
    session.flush()
    return listing


def suggestion_for(session, path, term: str, **kwargs):
    price_list = load_price_list(path)
    report = advise_limits(session, price_list, now=NOW, **kwargs)
    return next(item for item in report.suggestions if item.term == term)


class TestBeratung:
    def test_ohne_liste_gibt_es_nichts_nachzufuehren(self, db_session):
        report = advise_limits(db_session, load_price_list(""), now=NOW)
        assert report.components == 0
        assert report.suggestions == []
        assert "keine Kauf-Limit-Liste" in report.summary

    def test_ohne_beobachtungen_bleibt_das_limit_stehen(self, list_path, db_session):
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB")
        assert item.direction == DIRECTION_NO_DATA
        assert item.suggested_max_price is None
        assert item.observations == 0
        assert "mindestens" in item.reason
        assert item.current_max_price == 100.0

    def test_zu_wenige_beobachtungen_ergeben_keine_empfehlung(self, list_path, db_session):
        for total in (80.0, 90.0):
            add_listing(db_session, total=total)
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB", min_observations=5)
        assert item.direction == DIRECTION_NO_DATA
        assert item.suggested_max_price is None
        assert item.observations == 2

    def test_guenstigstes_zehntel_senkt_das_limit(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total)
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB", min_observations=5)
        assert item.direction == DIRECTION_LOWER
        assert item.observations == 5
        # 10. Perzentil von (80, 85, 90, 95, 100) = 82 -> auf 5 € gerundet: 80.
        assert item.suggested_max_price == 80.0
        assert item.q10 == 82.0
        assert item.median == 90.0

    def test_hoher_markt_hebt_das_limit_an(self, list_path, db_session):
        for total in (300.0, 310.0, 320.0, 330.0, 340.0):
            add_listing(db_session, total=total)
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB", min_observations=5)
        assert item.direction == DIRECTION_RAISE
        # 10. Perzentil = 304 -> auf 5 € gerundet 305, begrenzt auf +50 % = 150.
        assert item.suggested_max_price == 150.0
        assert "begrenzt" in item.reason

    def test_kleine_abweichung_laesst_das_limit_unveraendert(self, list_path, db_session):
        for total in (99.0, 100.0, 101.0, 102.0, 103.0):
            add_listing(db_session, total=total)
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB", min_observations=5)
        assert item.direction == DIRECTION_KEEP
        assert item.suggested_max_price == 100.0

    def test_nur_passende_angebote_zaehlen(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total)
        # Fremdes Modell, unklares Format und verschwundene Angebote zählen nicht.
        add_listing(db_session, title="RX 6800 XT 16GB", model="RX 6800 XT", total=70.0)
        add_listing(db_session, total=72.0, listing_format=FORMAT_UNKNOWN)
        add_listing(db_session, total=74.0, status=STATUS_GONE)
        # Defekte Ware ist kein Marktpreis für funktionierende Hardware.
        add_listing(db_session, total=10.0, condition_rank=0)
        item = suggestion_for(db_session, list_path, "RTX 3080 10GB", min_observations=5)
        assert item.observations == 5

    def test_alte_beobachtungen_fallen_aus_dem_fenster(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total, age_days=400)
        item = suggestion_for(
            db_session, list_path, "RTX 3080 10GB", min_observations=5, window_days=150
        )
        assert item.observations == 0
        assert item.direction == DIRECTION_NO_DATA

    def test_bericht_meldet_nur_echte_aenderungen(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total)
        price_list = load_price_list(list_path)
        report = advise_limits(db_session, price_list, min_observations=5, now=NOW)
        assert report.changes == 1
        assert report.with_data == 1
        assert report.actionable() == {0: 80.0}
        assert report.to_dict()["suggestions"][0]["change_pct"] == -20.0


class TestDateiSchreiben:
    def test_nur_zahlen_werden_ersetzt(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total)
        report = advise_limits(
            db_session, load_price_list(list_path), min_observations=5, now=NOW
        )
        written, backup = rewrite_price_limits(list_path, report.actionable())
        assert written == 1
        text = list_path.read_text(encoding="utf-8")
        assert "NVIDIA GeForce RTX 3080 10GB = 80 €" in text
        # Überschriften, Trennlinien und Hinweise bleiben unberührt.
        assert "PC-KOMPONENTEN - GEBRAUCHTMARKT-KAUFLIMITS" in text
        assert "Ein Hinweistext ohne Gleichheitszeichen." in text
        assert "Faustregel fuer Nachverhandlungen" in text
        # Die Sicherung enthält den alten Stand.
        backup_path = Path(backup)
        assert backup_path.is_file()
        assert backup_path.parent == list_path.parent
        assert "= 100 €" in backup_path.read_text(encoding="utf-8")

    def test_geschriebener_wert_wird_sofort_gelesen(self, list_path, db_session):
        for total in (80.0, 85.0, 90.0, 95.0, 100.0):
            add_listing(db_session, total=total)
        report = advise_limits(
            db_session, load_price_list(list_path), min_observations=5, now=NOW
        )
        rewrite_price_limits(list_path, report.actionable())
        reloaded = load_price_list(list_path)
        assert reloaded.limit_for(title="ASUS RTX 3080 10GB".replace("10GB", "10 GB")).max_price == 80.0

    def test_ohne_aenderungen_wird_nichts_geschrieben(self, list_path):
        before = list_path.read_bytes()
        written, backup = rewrite_price_limits(list_path, {})
        assert (written, backup) == (0, "")
        assert list_path.read_bytes() == before
        assert not list(Path(list_path).parent.glob("hoechstpreise_*"))

    def test_mehrere_abschnitte_werden_indexgenau_getroffen(self, tmp_path):
        path = tmp_path / "liste.txt"
        path.write_text(
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 = 100 €\n"
            "\n"
            "CPUs\n"
            "----------------------------------------------------------------\n"
            "AMD Ryzen 5 5600X = 70 €\n",
            encoding="utf-8",
        )
        entries = parse_price_limits(path.read_text(encoding="utf-8"))
        assert [entry.term for entry in entries] == ["RTX 3080", "Ryzen 5 5600X"]
        written, _backup = rewrite_price_limits(path, {1: 65.0})
        assert written == 1
        text = path.read_text(encoding="utf-8")
        assert "RTX 3080 = 100 €" in text
        assert "Ryzen 5 5600X = 65 €" in text


class TestProjektliste:
    def test_die_liste_des_projekts_deckt_neue_kategorien_ab(self):
        """Die erweiterten Kategorien müssen wirklich gelesen werden."""
        price_list = load_price_list(REPO_ROOT / DEFAULT_PRICE_LIST_FILE)
        assert {"gpu", "cpu", "ram", "ssd", "hdd", "mainboard", "psu", "cooler", "case"} <= (
            price_list.group_keys()
        )
        for entry in price_list:
            assert entry.term
            assert entry.max_price > 0


class TestApi:
    def test_vorschlaege_ohne_liste_liefern_einen_leeren_bericht(self, client):
        response = client.get("/api/price-limits/suggestions")
        assert response.status_code == 200
        body = response.json()
        assert body["components"] == 0
        assert body["suggestions"] == []
        assert body["disclaimer"]

    def test_uebernehmen_ohne_liste_wird_abgelehnt(self, client):
        response = client.post("/api/price-limits/apply")
        assert response.status_code == 422
        assert "Kauf-Limit-Liste" in response.json()["detail"]
