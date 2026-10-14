"""Anwendungszustand und FastAPI-Abhaengigkeiten.

Alle Komponenten werden einmal beim Start aufgebaut und geteilt. Das haelt
den Start schnell und verhindert, dass pro Anfrage neue Clients entstehen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import RuntimeSettings, Settings, get_settings
from .connectors.registry import ConnectorRegistry
from .db import create_db_engine, init_db
from .knowledge.base import KnowledgeBase
from .search.manager import SearchRunManager
from .services.settings_store import load_runtime_settings

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    knowledge: KnowledgeBase
    registry: ConnectorRegistry
    manager: SearchRunManager

    def knowledge_paths(self) -> list[str]:
        paths: list[str] = []
        base = Path(self.settings.knowledge_path)
        if base.is_dir():
            paths.append(str(base))
        elif base.is_file():
            paths.append(str(base))
        if self.settings.knowledge_user_file:
            paths.append(self.settings.knowledge_user_file)
        return paths

    def reload_knowledge(self):
        return self.knowledge.load(self.knowledge_paths())

    def runtime(self, session: Session) -> RuntimeSettings:
        return load_runtime_settings(session, self.settings)


def build_state(settings: Settings | None = None) -> AppState:
    settings = settings or get_settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    knowledge = KnowledgeBase()
    summary = knowledge.load(
        [
            *(
                [settings.knowledge_path]
                if Path(settings.knowledge_path).exists()
                else []
            ),
            *([settings.knowledge_user_file] if settings.knowledge_user_file else []),
        ]
    )
    logger.info(
        "Wissensbasis geladen: %s Eintraege, %s mit Preisdaten",
        summary.entries,
        summary.priced_entries,
    )
    for error in summary.errors:
        logger.warning("Wissensbasis: %s", error)

    registry = ConnectorRegistry(settings)
    manager = SearchRunManager(
        settings=settings,
        registry=registry,
        knowledge=knowledge,
        session_factory=session_factory,
    )
    return AppState(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        knowledge=knowledge,
        registry=registry,
        manager=manager,
    )


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "container", None)
    if state is None:  # pragma: no cover - nur bei Fehlkonfiguration
        raise RuntimeError("Anwendungszustand ist nicht initialisiert")
    return state


def get_session(request: Request) -> Iterator[Session]:
    state = get_state(request)
    session = state.session_factory()
    try:
        yield session
    finally:
        session.close()
