"""Die Erkennung eines veralteten laufenden Backends.

Das Backend vergleicht seinen Startzeitpunkt mit den Aenderungszeiten der
Quelldateien. Der Unterschied entscheidet, ob die Startskripte ein laufendes
Backend als "aktuell" hinnehmen duerfen oder auf einen Neustart bestehen
muessen - ein alter Prozess wuerde sonst weiter bedienen.
"""

from __future__ import annotations

from pathlib import Path

from backend import runtime_guard


def test_frischer_code_ist_nicht_veraltet(tmp_path: Path, monkeypatch):
    source = tmp_path / "modul.py"
    source.write_text("x = 1\n", encoding="utf-8")
    mtime = source.stat().st_mtime

    # Der Prozess startete nach der Aenderung: Er hat den aktuellen Stand.
    monkeypatch.setattr(runtime_guard, "STARTED_AT", mtime + 1.0)
    assert runtime_guard.newer_sources(tmp_path) == []
    assert runtime_guard.is_stale(tmp_path) is False


def test_datei_genau_zum_start_zaehlt_nicht_als_neuer(tmp_path: Path, monkeypatch):
    """Gleiche Zeit heisst: Der Prozess hat die Datei noch geladen."""
    source = tmp_path / "modul.py"
    source.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(runtime_guard, "STARTED_AT", source.stat().st_mtime)
    assert runtime_guard.is_stale(tmp_path) is False


def test_nach_dem_start_geaenderte_datei_macht_veraltet(tmp_path: Path, monkeypatch):
    source = tmp_path / "search" / "titles.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")
    mtime = source.stat().st_mtime

    # Startzeitpunkt liegt vor der Aenderung: Der Prozess hat den alten Stand.
    monkeypatch.setattr(runtime_guard, "STARTED_AT", mtime - 1.0)
    assert runtime_guard.newer_sources(tmp_path) == [str(Path("search") / "titles.py")]
    assert runtime_guard.is_stale(tmp_path) is True


def test_cachedateien_zaehlen_nicht(tmp_path: Path, monkeypatch):
    """Nur ausgelieferter Code zaehlt - __pycache__ und Fremddateien nicht."""
    cached = tmp_path / "__pycache__" / "modul.cpython-311.pyc"
    cached.parent.mkdir()
    cached.write_bytes(b"cache")
    (tmp_path / "notiz.md").write_text("notiz", encoding="utf-8")

    monkeypatch.setattr(runtime_guard, "STARTED_AT", 0.0)
    assert runtime_guard.newer_sources(tmp_path) == []
    assert runtime_guard.source_files(tmp_path) == []


def test_status_berichtet_startzeitpunkt_und_liste(monkeypatch):
    monkeypatch.setattr(runtime_guard, "STARTED_AT", 0.0)
    report = runtime_guard.status(runtime_guard.BACKEND_DIR)

    assert set(report) == {"started_at", "stale", "newer_sources"}
    assert report["started_at"].endswith("+00:00")
    # STARTED_AT=0 liegt vor allen Dateien: alles ist "neuer" als der Start.
    assert report["stale"] is True
    assert report["newer_sources"]
    assert len(report["newer_sources"]) <= 10
    assert all(name.endswith(".py") for name in report["newer_sources"])


def test_health_meldet_den_laufenden_prozess(client):
    """/api/health muss sagen, mit welchem Codestand der Prozess laeuft."""
    runtime = client.get("/api/health").json()["runtime"]

    assert set(runtime) == {"started_at", "stale", "newer_sources"}
    assert isinstance(runtime["stale"], bool)
    # Im Testprozess wird nichts nach dem Start geschrieben: also aktuell.
    assert runtime["stale"] is False
