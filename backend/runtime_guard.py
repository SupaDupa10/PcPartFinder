"""Erkennt, ob auf der Platte neuerer Code liegt als im laufenden Prozess.

Ein Backend, das ohne ``--reload`` läuft, kann geänderten Python-Code nicht
laden: Es arbeitet weiter mit dem Stand vom Startzeitpunkt. Das ist genau
der Fall, der leicht übersehen wird - ``start.bat``/``start.sh`` sehen nur
"Port belegt, es antwortet unser Backend" (Exit-Code 3) und starten nichts
Neues. Danach gilt eine entfernte Funktion weiter und eine neue Einstellung
wirkt nicht, obwohl im Repository längst etwas anderes steht.

Dieses Modul macht den Unterschied sichtbar: Der laufende Prozess vergleicht
seinen Startzeitpunkt mit den Änderungszeiten der Quelldateien und meldet das
Ergebnis über ``/api/health``. Die Startskripte können daran erkennen, dass
ein Neustart nötig ist (Exit-Code 5 der Portprüfung).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

# Das Backend-Verzeichnis mit dem ausgelieferten Python-Code.
BACKEND_DIR = Path(__file__).resolve().parent

SOURCE_SUFFIX: tuple[str, ...] = (".py",)

# Ordner, die keinen ausgelieferten Code enthalten.
SKIP_DIR_NAMES = frozenset({"__pycache__"})

# Zeitpunkt des Prozessstarts (beim Import festgehalten). Alles, was danach
# geschrieben wurde, hat dieser Prozess nicht mehr geladen.
STARTED_AT = time.time()


def started_at_iso() -> str:
    """Startzeitpunkt des Prozesses in UTC (ISO 8601)."""
    return datetime.fromtimestamp(STARTED_AT, tz=timezone.utc).isoformat()


def source_files(root: Path | None = None) -> list[Path]:
    """Alle ausgelieferten Quelldateien unterhalb von ``root``."""
    base = root or BACKEND_DIR
    found: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in SOURCE_SUFFIX:
            found.append(path)
    return sorted(found)


def newer_sources(root: Path | None = None) -> list[str]:
    """Quelldateien, die nach dem Prozessstart geschrieben wurden.

    Rückgabe sind Pfade relativ zu ``root`` (bzw. relativ zum Backend), damit
    die Meldung kurz bleibt. Ein Fehler beim Lesen einer Zeitangabe führt nicht
    zum Abbruch: Eine verschwundene Datei ist kein veralteter Prozess.
    """
    base = root or BACKEND_DIR
    changed: list[tuple[float, str]] = []
    for path in source_files(base):
        try:
            mtime = path.stat().st_mtime
        except OSError:  # pragma: no cover - Datei zwischen zwei Aufrufen weg
            continue
        if mtime <= STARTED_AT:
            continue
        try:
            name = str(path.relative_to(base))
        except ValueError:  # pragma: no cover - defensive
            name = str(path)
        changed.append((mtime, name))
    return [name for _mtime, name in sorted(changed)]


def is_stale(root: Path | None = None) -> bool:
    """Läuft der Prozess mit älterem Code als auf der Platte?"""
    return bool(newer_sources(root))


def status(root: Path | None = None) -> dict:
    """Kurzbericht für ``/api/health``."""
    changed = newer_sources(root)
    return {
        "started_at": started_at_iso(),
        "stale": bool(changed),
        "newer_sources": changed[:10],
    }
