"""Die mitgelieferten Beispieldaten (``samples/beispiel_angebote.json``).

Sie sind der Ersatz, wenn keine eigene Datenquelle konfiguriert ist: Ohne sie
waere beim ersten Start keine Plattform nutzbar (eBay braucht Zugangsdaten,
Kleinanzeigen hat keine zulaessige API). Deshalb muss die Datei gueltig sein und
ehrlich als Demo gekennzeichnet bleiben.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

from backend.connectors.base import SearchQuerySpec
from backend.connectors.local_import import LocalImportConnector

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "samples" / "beispiel_angebote.json"


@pytest.fixture(scope="module")
def document() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


class TestBeispielDatei:
    def test_datei_existiert_und_ist_gueltiges_json(self, document):
        assert SAMPLE.is_file()
        assert isinstance(document.get("listings"), list)
        assert len(document["listings"]) >= 10

    def test_deklariert_sich_als_beispieldaten(self, document):
        hinweis = document.get("_hinweis", "")
        assert "BEISPIEL" in hinweis.upper()
        # Ehrlichkeit: Die Datei darf nicht als echte Datenquelle durchgehen.
        assert "erfunden" in hinweis or "erfunden" in hinweis.lower()

    def test_alle_eintraege_haben_pflichtfelder_und_realistische_werte(self, document):
        seen: set[str] = set()
        for item in document["listings"]:
            assert item["title"].strip()
            assert item["url"].startswith("https://example.invalid/"), item["url"]
            assert item["id"] not in seen, f"doppelte ID: {item['id']}"
            seen.add(item["id"])
            assert float(item["price"]) > 0
            # Keine echten Plattformlinks: Die Datei darf niemanden auf eine
            # echte Anzeige schicken.
            for key, value in item.items():
                if isinstance(value, str) and value.startswith("http"):
                    assert "example.invalid" in value, value

    def test_kategorien_sind_abgedeckt(self, document):
        titles = " ".join(item["title"].lower() for item in document["listings"])
        for begriff in ("grafikkarte", "prozessor", "arbeitsspeicher", "ssd", "mainboard", "netzteil"):
            assert begriff in titles, f"Beispiel fehlt fuer: {begriff}"

    def test_enthaelt_auch_unpassende_angebote(self, document):
        """Filter und Risikoerkennung sollen an den Beispieldaten etwas zu tun haben."""
        options = [opt for item in document["listings"] for opt in item.get("buying_options", [])]
        assert "AUCTION" in options, "Ohne Auktion fehlt der Test fuer den Ausschluss"
        texte = " ".join(item.get("description", "") for item in document["listings"]).lower()
        assert "defekt" in texte, "Ohne Mangel fehlt der Test fuer die Risikoerkennung"


class TestConnectorMitBeispieldaten:
    @pytest.mark.asyncio()
    async def test_beispieldaten_liefern_treffer(self):
        from backend.config import get_settings

        connector = LocalImportConnector(get_settings(), path=str(SAMPLE))
        assert connector.capabilities.available is True
        page = await connector.search_page(
            SearchQuerySpec(term="grafikkarte", platform="local_import", page_size=100)
        )
        assert page.listings, "Die Beispieldaten liefern keine Treffer"

    @pytest.mark.asyncio()
    async def test_fehlende_datei_meldet_grund(self, tmp_path):
        from backend.config import get_settings

        connector = LocalImportConnector(get_settings(), path=str(tmp_path / "fehlt.json"))
        caps = connector.capabilities
        assert caps.available is False
        assert caps.reason_unavailable


def test_startskripte_aktivieren_beispieldaten_als_rueckfall():
    shell = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
    batch = (REPO_ROOT / "start.bat").read_text(encoding="utf-8")
    assert "samples/beispiel_angebote.json" in shell
    assert "samples\\beispiel_angebote.json" in batch
    # Eine eigene Angabe muss Vorrang haben.
    assert "LOCAL_IMPORT_PATH" in shell and "LOCAL_IMPORT_PATH" in batch


def test_beispieldaten_greifen_nicht_bei_vorhandener_ebay_konfiguration():
    """Mit eBay-Zugangsdaten duerfen sich keine Demo-Angebote beimischen."""
    shell = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
    batch = (REPO_ROOT / "start.bat").read_text(encoding="utf-8")
    assert "EBAY_CLIENT_ID" in shell
    assert "EBAY_CLIENT_ID" in batch
    # In beiden Skripten wird vor dem Demo-Zweig auf eBay geprueft.
    assert shell.index("EBAY_CLIENT_ID") < shell.index("samples/beispiel_angebote.json")
    assert batch.index("EBAY_CLIENT_ID") < batch.index("samples\\beispiel_angebote.json")


class TestNachladenOhneNeustart:
    """Nach dem Umwandeln einer Seite muss die App die Datei neu einlesen.

    Ohne das müsste man nach jedem Import die App neu starten - der manuelle
    Weg (eigene Suche speichern, umwandeln, Suche starten) wäre damit praktisch
    unbrauchbar.
    """

    @staticmethod
    def _write(path: pathlib.Path, titles: list[str]) -> None:
        listings = [
            {
                "id": f"eintrag-{index}",
                "title": title,
                "url": f"https://example.invalid/angebot/{index}",
                "price": 100.0 + index,
            }
            for index, title in enumerate(titles)
        ]
        path.write_text(json.dumps({"listings": listings}), encoding="utf-8")

    @pytest.mark.asyncio()
    async def test_geaenderte_datei_wird_neu_gelesen(self, tmp_path):
        from backend.config import get_settings
        from backend.connectors.base import SearchQuerySpec
        from backend.connectors.local_import import LocalImportConnector

        target = tmp_path / "angebote.json"
        self._write(target, ["Grafikkarte RTX 3080"])
        connector = LocalImportConnector(get_settings(), path=str(target))
        query = SearchQuerySpec(term="grafikkarte", platform="local_import", page_size=50)

        first = await connector.search_page(query)
        assert len(first.listings) == 1

        self._write(target, ["Grafikkarte RTX 3080", "Grafikkarte RTX 3070 neu"])
        second = await connector.search_page(query)
        assert len(second.listings) == 2, "Änderung an der Importdatei wurde nicht erkannt"

    @pytest.mark.asyncio()
    async def test_unveraenderte_datei_bleibt_im_cache(self, tmp_path):
        """Unnötiges Neu-Einlesen bleibt aus (gleiche Datei, gleicher Inhalt)."""
        from backend.config import get_settings
        from backend.connectors.local_import import LocalImportConnector

        target = tmp_path / "angebote.json"
        self._write(target, ["Grafikkarte RTX 3080"])
        connector = LocalImportConnector(get_settings(), path=str(target))
        first = connector._load()
        second = connector._load()
        assert first is second, "Ohne Änderung darf die Datei nicht erneut geparst werden"

    @pytest.mark.asyncio()
    async def test_geloeschte_datei_meldet_grund(self, tmp_path):
        from backend.config import get_settings
        from backend.connectors.base import SearchQuerySpec
        from backend.connectors.local_import import LocalImportConnector, ConnectorError

        target = tmp_path / "angebote.json"
        self._write(target, ["Grafikkarte RTX 3080"])
        connector = LocalImportConnector(get_settings(), path=str(target))
        await connector.search_page(
            SearchQuerySpec(term="grafikkarte", platform="local_import", page_size=10)
        )
        target.unlink()
        with pytest.raises(ConnectorError):
            await connector.search_page(
                SearchQuerySpec(term="grafikkarte", platform="local_import", page_size=10)
            )
