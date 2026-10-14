"""Der Offline-Import aus gespeicherten Suchseiten.

Das Werkzeug stellt selbst keine Anfragen an Plattformen - geprueft wird, dass
es gespeichertes HTML zuverlaessig in das Importformat der App ueberfuehrt und
dabei ehrlich meldet, was es nicht erkennen konnte.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "import_html.py"

PAGE_WITH_JSON_LD = """<!DOCTYPE html>
<html lang="de"><head>
  <link rel="canonical" href="https://www.kleinanzeigen.de/s-pc-netzwerk/grafikkarte/k0">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
    {"@type":"ListItem","item":{"@type":"Product","name":"ASUS RTX 3080 TUF Gaming",
      "url":"https://www.kleinanzeigen.de/s-anzeige/asus-rtx-3080-tuf/2711111-225-8031",
      "offers":{"@type":"Offer","price":"215.00","priceCurrency":"EUR"}}},
    {"@type":"ListItem","item":{"@type":"Product","name":"MSI RTX 3080 Gaming X Trio",
      "url":"/s-anzeige/msi-rtx-3080-gaming-x/2722222-225-1015",
      "offers":{"@type":"Offer","price":"268,50","priceCurrency":"EUR"}}}
  ]}
  </script>
  </head><body><h1>Grafikkarte in PC &amp; Netzwerk</h1></body></html>
"""

PAGE_WITHOUT_JSON_LD = """<!DOCTYPE html>
<html lang="de"><head><title>Grafikkarte</title></head><body>
<div class="ad-list">
  <article class="aditem">
    <a class="ellipsis" href="/s-anzeige/asus-rtx-3080-tuf/2711111-225-8031">ASUS RTX 3080 TUF Gaming 10GB</a>
    <div>215 € VB</div><div>80331 München</div><div>02.10.2026</div>
  </article>
  <article class="aditem">
    <a class="ellipsis" href="/s-anzeige/msi-rtx-3080-gaming-x/2722222-225-1015">MSI RTX 3080 Gaming X Trio</a>
    <div>268,50 € VB</div><div>10115 Berlin</div>
  </article>
  <article class="aditem">
    <a class="ellipsis" href="/s-anzeige/leerer-text-kurz/x">Kurz</a>
    <div>Zu verschenken</div>
  </article>
</div>
</body></html>
"""


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture()
def pages(tmp_path: Path) -> Path:
    (tmp_path / "mit_jsonld.html").write_text(PAGE_WITH_JSON_LD, encoding="utf-8")
    (tmp_path / "ohne_jsonld.html").write_text(PAGE_WITHOUT_JSON_LD, encoding="utf-8")
    (tmp_path / "leer.html").write_text("<html><body>Keine Treffer</body></html>", encoding="utf-8")
    return tmp_path


class TestAusSchemaDaten:
    def test_json_ld_wird_zu_importformat(self, pages, tmp_path):
        target = tmp_path / "out.json"
        result = run_tool(str(pages / "mit_jsonld.html"), "-o", str(target))
        assert result.returncode == 0, result.stdout + result.stderr
        document = json.loads(target.read_text(encoding="utf-8"))
        listings = document["listings"]
        assert len(listings) == 2
        first = listings[0]
        assert first["title"] == "ASUS RTX 3080 TUF Gaming"
        assert first["price"] == 215.0
        assert first["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
        # Relative Links werden absolut gemacht, deutsche Preisschreibweise verstanden.
        assert listings[1]["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
        assert listings[1]["price"] == 268.5
        assert "selbst gespeicherten" in document["_hinweis"]

    def test_json_ld_wird_bevorzugt_vor_linkerkennung(self, pages, tmp_path):
        target = tmp_path / "out.json"
        result = run_tool(str(pages / "mit_jsonld.html"), "-o", str(target), "--debug")
        assert "schema.org" in result.stdout


class TestOhneSchemaDaten:
    def test_links_mit_preis_ort_und_datum(self, pages, tmp_path):
        target = tmp_path / "out.json"
        result = run_tool(str(pages / "ohne_jsonld.html"), "-o", str(target), "--debug")
        assert result.returncode == 0, result.stdout + result.stderr
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        urls = [entry["url"] for entry in listings]
        assert len(urls) == 2, f"Erwartet: 2 brauchbare Anzeigen, gefunden: {urls}"
        first = listings[0]
        assert first["title"] == "ASUS RTX 3080 TUF Gaming 10GB"
        assert first["price"] == 215.0
        assert first["postal_code"] == "80331"
        assert first["location"].startswith("München")
        assert first["published_at"].startswith("2026-10-02")
        assert listings[1]["price"] == 268.5
        assert "Anzeigenlinks erkannt" in result.stdout

    def test_zu_kurze_anzeigentexte_werden_uebersprungen(self, pages, tmp_path):
        target = tmp_path / "out.json"
        run_tool(str(pages / "ohne_jsonld.html"), "-o", str(target))
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        assert all("leerer-text-kurz" not in entry["url"] for entry in listings)


class TestVerhalten:
    def test_leere_seite_meldet_keinen_treffer(self, pages, tmp_path):
        target = tmp_path / "out.json"
        result = run_tool(str(pages / "leer.html"), "-o", str(target), "--debug")
        assert result.returncode == 1
        assert "nichts erkannt" in result.stdout
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["listings"] == []

    def test_merge_ergaenzt_ohne_dubletten(self, pages, tmp_path):
        target = tmp_path / "out.json"
        run_tool(str(pages / "mit_jsonld.html"), "-o", str(target))
        result = run_tool(str(pages / "mit_jsonld.html"), "-o", str(target), "--merge")
        assert result.returncode == 0
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        assert len(listings) == 2, "Dieselben URLs duerfen nicht doppelt landen"
        # Zweite, andere Seite ergaenzt.
        result = run_tool(str(pages / "ohne_jsonld.html"), "-o", str(target), "--merge")
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        assert len(listings) == 2, "Gleiche Anzeigen aus anderer Quelle bleiben einmalig"
        assert result.returncode == 0

    def test_fehlende_datei_wird_gemeldet(self, tmp_path):
        result = run_tool(str(tmp_path / "gibt-es-nicht.html"), "-o", str(tmp_path / "out.json"))
        assert result.returncode == 1
        assert "nicht gefunden" in result.stderr

    def test_ergebnis_ist_mit_dem_connector_lesbar(self, pages, tmp_path):
        """Das erzeugte JSON muss der lokale Import-Connector einlesen können."""
        import asyncio

        from backend.config import get_settings
        from backend.connectors.local_import import LocalImportConnector

        target = tmp_path / "out.json"
        run_tool(str(pages / "ohne_jsonld.html"), "-o", str(target))
        connector = LocalImportConnector(get_settings(), path=str(target))
        assert connector.capabilities.available is True
        listings = asyncio.run(connector.test_connection())
        assert listings[0] is True
        assert listings[2]["angebote"] == 2

    def test_werkzeug_ruft_keine_plattform_auf(self):
        """Reine Offline-Verarbeitung: kein HTTP-Client im Skript."""
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("httpx", "requests", "urllib.request", "httplib", "socket"):
            assert forbidden not in source, f"{forbidden} hat in einem Offline-Werkzeug nichts zu suchen"


# eBay-Suchseiten: Links heissen /itm/<id>, Preise stehen teils vor dem Betrag,
# der volle Titel steht oft im title-Attribut des Links.
EBAY_PAGE = """<!DOCTYPE html>
<html lang="de"><head><title>rtx 3080 | eBay</title></head><body>
<ul class="srp-results">
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.de/itm/123456789012?hash=abc"
       title="ASUS GeForce RTX 3080 TUF Gaming 10GB Grafikkarte TOP">
      ASUS RTX 3080 TUF
    </a>
    <span class="s-item__price">EUR 210,00</span>
    <span class="s-item__location">aus München</span>
    <span class="SECONDARY_INFO">Gebraucht</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="/itm/223344556677" title="MSI GeForce RTX 3080 Gaming X Trio 10GB">
      MSI RTX 3080
    </a>
    <span class="s-item__price">268,50 €</span>
    <span class="s-item__location">aus Hamburg</span>
    <span class="SECONDARY_INFO">Gebraucht</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="/itm/999999999999" title="Mehr">Mehr</a>
    <span class="s-item__price">EUR 10,00</span>
  </li>
</ul>
</body></html>
"""


class TestEbaySeite:
    def test_ebay_links_werden_erkannt(self, tmp_path):
        page = tmp_path / "ebay.html"
        page.write_text(EBAY_PAGE, encoding="utf-8")
        target = tmp_path / "out.json"
        result = run_tool(str(page), "-o", str(target), "--debug")
        assert result.returncode == 0, result.stdout + result.stderr
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        assert len(listings) == 2, [entry["url"] for entry in listings]
        first = listings[0]
        # Vollstaendiger Titel kommt aus dem title-Attribut.
        assert first["title"] == "ASUS GeForce RTX 3080 TUF Gaming 10GB Grafikkarte TOP"
        assert first["url"] == "https://www.ebay.de/itm/123456789012"
        # "EUR 210,00" (Betrag nach der Waehrung) muss ebenfalls verstanden werden.
        assert first["price"] == 210.0
        assert first["location"] == "München"
        assert listings[1]["price"] == 268.5
        assert listings[1]["url"].startswith("https://www.ebay.de/itm/")

    def test_navigationslinks_werden_uebersprungen(self, tmp_path):
        page = tmp_path / "ebay.html"
        page.write_text(EBAY_PAGE, encoding="utf-8")
        target = tmp_path / "out.json"
        run_tool(str(page), "-o", str(target))
        listings = json.loads(target.read_text(encoding="utf-8"))["listings"]
        assert all("999999999999" not in entry["url"] for entry in listings)


class TestBetragsErkennung:
    """Preise duerfen nicht ueber Wortgrenzen hinweg gelesen werden."""

    @staticmethod
    def _module():
        import importlib.util

        spec = importlib.util.spec_from_file_location("import_html", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_deutsche_schreibweisen(self):
        module = self._module()
        assert module.to_float("210,00") == 210.0
        assert module.to_float("1.234,56") == 1234.56
        assert module.to_float("268") == 268.0

    def test_benachbarter_titel_verfaelscht_den_preis_nicht(self):
        module = self._module()
        match = module.PRICE_RE.search("MSI RTX 3080 268,50 € aus Hamburg")
        assert match is not None
        assert (match.group("value1") or match.group("value2")) == "268,50"

    def test_waehrung_vor_dem_betrag_mit_tausenderpunkt(self):
        module = self._module()
        match = module.PRICE_RE.search("EUR 1.234,56")
        assert match is not None
        assert (match.group("value1") or match.group("value2")) == "1.234,56"
