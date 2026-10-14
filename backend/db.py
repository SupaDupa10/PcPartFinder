"""Datenbank-Setup: Engine, Session-Factory, Initialisierung und Migrationen.

Migrationen werden bewusst schlank gehalten: Die Anwendung legt fehlende
Tabellen an und ergänzt fehlende Spalten (``ALTER TABLE ADD COLUMN``). Das
vermeidet eine schwere Alembic-Abhaengigkeit für eine lokale Einzelplatz-App,
bleibt aber idempotent und datenerhaltend.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .models import Base, Listing, PriceHistory, SearchQuery, SearchRun, utcnow

logger = logging.getLogger(__name__)


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix) :]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str | None = None) -> Engine:
    settings: Settings = get_settings()
    db_url = url or settings.database_url
    _ensure_sqlite_dir(db_url)
    kwargs: dict = {"future": True}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(db_url, **kwargs)

    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def reset_engine_state() -> None:
    """Nur für Tests: setzt die globalen Engine-/Session-Objekte zurueck."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaktionaler Session-Kontext."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def apply_lightweight_migrations(engine: Engine) -> list[str]:
    """Ergaenzt fehlende Spalten bestehender Tabellen.

    Gibt die Liste der durchgeführten Änderungen zurueck (für Tests/Logs).
    """
    changes: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if column.primary_key:
                    continue
                ddl = _column_ddl(column)
                if ddl is None:
                    continue
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
                changes.append(f"{table.name}.{column.name}")
    return changes


def _column_ddl(column) -> str | None:
    """Erzeugt eine einfache SQLite-Spalten-Definition ohne Fremdschluessel."""
    from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text

    type_map = {
        String: "VARCHAR",
        Text: "TEXT",
        Integer: "INTEGER",
        Float: "REAL",
        Boolean: "BOOLEAN",
        DateTime: "DATETIME",
        JSON: "JSON",
    }
    sql_type = None
    for py_type, sql_name in type_map.items():
        if isinstance(column.type, py_type):
            sql_type = sql_name
            break
    if sql_type is None:
        return None
    return f'"{column.name}" {sql_type}'


def init_db(engine: Engine | None = None) -> Engine:
    """Legt Tabellen an und führt leichte Migrationen aus."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    changes = apply_lightweight_migrations(engine)
    if changes:
        logger.info("DB-Migration durchgeführt: %s", ", ".join(changes))
    return engine


def clear_stored_data(session: Session) -> dict[str, int]:
    """Leert Angebote und Suchverlauf vollstaendig ("alles auf null").

    Gedacht als bewusster Neuanfang: Die gespeicherten Angebote stammen aus
    frueheren Suchen und bleiben sonst fuer immer in der Ergebnisliste stehen -
    auch dann, wenn die Titelpruefung inzwischen besser aussortiert. Die
    Reihenfolge richtet sich nach den Fremdschluesseln (Anfragen und
    Preisverlauf zuerst). Einstellungen und Wissensbasis bleiben unberuehrt.
    """
    removed: dict[str, int] = {}

    removed["search_queries"] = session.execute(
        delete(SearchQuery)
    ).rowcount or 0
    removed["price_history"] = session.execute(
        delete(PriceHistory)
    ).rowcount or 0
    removed["search_runs"] = session.execute(delete(SearchRun)).rowcount or 0
    removed["listings"] = session.execute(delete(Listing)).rowcount or 0
    session.commit()
    return removed


def cleanup_old_records(session: Session, retention_days: int) -> dict[str, int]:
    """Bereinigt alte Datensaetze (Abschnitt 8: Aufbewahrungsfristen).

    Es werden nur vergangene Suchläufe und deren Query-Protokolle entfernt.
    Angebote selbst bleiben erhalten, solange sie nicht als nicht mehr
    verfügbar markiert sind UND alt sind.
    """
    now = utcnow()
    cutoff = now - timedelta(days=retention_days)

    old_runs = session.scalars(select(SearchRun).where(SearchRun.created_at < cutoff)).all()
    removed_runs = 0
    for run in old_runs:
        session.delete(run)
        removed_runs += 1

    gone_cutoff = now - timedelta(days=30)
    removed_listings = 0
    stale = session.scalars(
        select(Listing).where(Listing.status == "gone", Listing.last_seen_at < gone_cutoff)
    ).all()
    for listing in stale:
        session.delete(listing)
        removed_listings += 1

    session.commit()
    return {
        "search_runs": removed_runs,
        "listings": removed_listings,
    }
