"""Gemeinsame Test-Fixtures.

Grundsaetze fuer alle Tests:

* Es wird NIEMALS eine echte Plattform angesprochen. Alle Angebotsdaten
  stammen aus lokalen Fixtures (``tests/fixtures``) oder werden im Test
  erzeugt.
* Die Anwendung arbeitet vollstaendig deterministisch - es gibt keinen
  KI-Pfad und damit nichts, was von einem Modell abhaengt.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LOCAL_IMPORT_FIXTURE = FIXTURES_DIR / "local_import_sample.json"
KNOWLEDGE_PATH = REPO_ROOT / "knowledge"


# ---------------------------------------------------------------------------
# Umgebung VOR dem Import der Anwendung setzen.
# ---------------------------------------------------------------------------
# ``backend.config.get_settings`` ist mit lru_cache versehen, deshalb muss die
# Umgebung bereits beim ersten Zugriff stimmen. Ein Test-Datenbankschema wird
# nur bei Bedarf erzeugt - die Unit-Tests arbeiten in-memory.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("KNOWLEDGE_PATH", str(KNOWLEDGE_PATH))
os.environ.setdefault("LOCAL_IMPORT_PATH", str(LOCAL_IMPORT_FIXTURE))
os.environ.setdefault("EBAY_CLIENT_ID", "")
os.environ.setdefault("EBAY_CLIENT_SECRET", "")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Die Kauf-Limit-Liste des Benutzers bleibt in Tests aussen vor: Tests, die sie
# brauchen, zeigen ausdruecklich auf eine eigene Datei.
os.environ.setdefault("PRICE_LIST_PATH", "")


@pytest.fixture(scope="session")
def settings():
    from backend.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def knowledge(settings):
    """Die echte Projekt-Wissensbasis (nur lesend verwendet)."""
    from backend.knowledge.base import KnowledgeBase

    base = KnowledgeBase()
    summary = base.load([settings.knowledge_path])
    assert summary.entries > 0, "Die Wissensbasis im Repository ist leer"
    return base


@pytest.fixture()
def resolver(knowledge):
    from backend.domain.hardware import HardwareResolver

    return HardwareResolver(knowledge)


@pytest.fixture()
def db_session() -> Iterator:
    """Frische In-Memory-Datenbank pro Test."""
    from sqlalchemy.orm import sessionmaker

    from backend.db import create_db_engine, init_db

    engine = create_db_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def app_state(tmp_path, monkeypatch):
    """Vollstaendig aufgebauter Anwendungszustand mit eigener Test-Datenbank."""
    from backend.config import get_settings
    from backend.deps import build_state

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KNOWLEDGE_PATH", str(KNOWLEDGE_PATH))
    monkeypatch.setenv("LOCAL_IMPORT_PATH", str(LOCAL_IMPORT_FIXTURE))
    monkeypatch.setenv("EBAY_CLIENT_ID", "")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "")
    monkeypatch.setenv("PRICE_LIST_PATH", "")
    get_settings.cache_clear()

    settings = get_settings()
    state = build_state(settings)
    try:
        yield state
    finally:
        state.engine.dispose()
        get_settings.cache_clear()


@pytest.fixture()
def client(app_state):
    """TestClient gegen die echte FastAPI-Anwendung (gleicher Zustand)."""
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app()
    app.state.container = app_state
    with TestClient(app) as test_client:
        yield test_client
