"""SQLAlchemy-Modelle (SQLite).

Bewusst mit SQLAlchemy 2.0 statt SQLModel: identische Funktionalitaet, aber
weniger Abhaengigkeiten und besser vorhersagbares Verhalten auf neuen
Python-Versionen. Pydantic wird separat für die API-Schemas genutzt.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- Konstanten (als Strings, damit SQLite-Dumps lesbar bleiben) -----------

# Angebotsformat (Abschnitt 7: Auktionen konsequent ausschließen)
FORMAT_FIXED_PRICE = "fixed_price"
FORMAT_AUCTION = "auction"
FORMAT_UNKNOWN = "unknown"

STATUS_ACTIVE = "active"
STATUS_UNKNOWN = "unknown"
STATUS_GONE = "gone"
# Vom Bestand aussortiert: Der Titel verkauft nicht das gesuchte Produkt
# (Teil, Verpackung, fremdes Gerät, Suchgesuch). Der Datensatz bleibt erhalten,
# damit die Entscheidung nachvollziehbar ist - angezeigt wird er nicht mehr.
STATUS_REJECTED = "rejected"

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_COMPLETED = "completed"
RUN_CANCELLED = "cancelled"
RUN_FAILED = "failed"


class Listing(Base):
    """Ein normalisiertes Angebot. Eindeutig je (Plattform, Plattform-ID)."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    platform: Mapped[str] = mapped_column(String(32), index=True)
    platform_listing_id: Mapped[str | None] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    # Preise
    price: Mapped[float | None] = mapped_column(Float)
    shipping_cost: Mapped[float | None] = mapped_column(Float)
    shipping_cost_known: Mapped[bool] = mapped_column(Boolean, default=False)
    total_price: Mapped[float | None] = mapped_column(Float, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")

    # Zeitangaben (None = unbekannt, wird nicht als "aktuell" vorgetäuscht)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at_known: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Verkäufer / Standort
    seller_name: Mapped[str | None] = mapped_column(String(128))
    seller_feedback_percent: Mapped[float | None] = mapped_column(Float)
    seller_feedback_score: Mapped[int | None] = mapped_column(Integer)
    location_text: Mapped[str | None] = mapped_column(String(256))
    postal_code: Mapped[str | None] = mapped_column(String(16), index=True)
    country: Mapped[str | None] = mapped_column(String(8))

    # Zustand
    condition_text: Mapped[str | None] = mapped_column(String(128))
    condition_rank: Mapped[int | None] = mapped_column(Integer)  # 0..4, None = unbekannt

    # Hardware-Erkennung (Abschnitt 11)
    category: Mapped[str | None] = mapped_column(String(32), index=True)
    manufacturer: Mapped[str | None] = mapped_column(String(64), index=True)
    product_family: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(96), index=True)
    variant: Mapped[str | None] = mapped_column(String(96))
    model_candidates: Mapped[list | None] = mapped_column(JSON)

    # Angebotsformat / Verfügbarkeit
    listing_format: Mapped[str] = mapped_column(String(16), default=FORMAT_UNKNOWN, index=True)
    format_evidence: Mapped[str | None] = mapped_column(String(256))
    # Bietet der Verkäufer Abholung an? Wird nicht geraten: nur wenn die
    # Quelle es meldet oder Beschreibung/Versanddaten es belegen.
    pickup_available: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
    # Grund der Aussortierung (nur gesetzt, wenn ``status`` ``rejected`` ist).
    reject_reason: Mapped[str | None] = mapped_column(String(256))

    # Bewertung
    market_value: Mapped[float | None] = mapped_column(Float)
    deal_score: Mapped[float | None] = mapped_column(Float, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str | None] = mapped_column(String(32), index=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    reasons: Mapped[list | None] = mapped_column(JSON)
    warnings: Mapped[list | None] = mapped_column(JSON)
    market_basis: Mapped[dict | None] = mapped_column(JSON)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Fallback-Deduplizierung: inhaltlicher Fingerabdruck (Plattform, Titel,
    # gerundeter Gesamtpreis, Verkäufer).
    dedup_fingerprint: Mapped[str | None] = mapped_column(String(160), index=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)

    # Bilder
    image_url: Mapped[str | None] = mapped_column(Text)

    # Favoriten
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    favorite_note: Mapped[str | None] = mapped_column(Text)

    # Suchlauf-Herkunft
    first_seen_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    last_seen_run_id: Mapped[int | None] = mapped_column(Integer, index=True)

    raw: Mapped[dict | None] = mapped_column(JSON)

    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("platform", "platform_listing_id", name="uq_listing_platform_id"),
        Index("ix_listing_dedup", "platform", "listing_format", "total_price"),
    )


class PriceHistory(Base):
    """Preisänderungen eines Angebots (Abschnitt 8: Preisänderung erkennen)."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    price: Mapped[float | None] = mapped_column(Float)
    shipping_cost: Mapped[float | None] = mapped_column(Float)
    total_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(16))

    listing: Mapped[Listing] = relationship(back_populates="price_history")


class SearchRun(Base):
    """Ein Suchlauf inkl. Fortschritt, Abdeckung und Fehlern."""

    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=RUN_PENDING, index=True)

    request: Mapped[dict] = mapped_column(JSON)
    plan: Mapped[dict | None] = mapped_column(JSON)
    plan_source: Mapped[str | None] = mapped_column(String(16))  # deterministic | price_list

    progress: Mapped[dict | None] = mapped_column(JSON)
    coverage: Mapped[dict | None] = mapped_column(JSON)
    errors: Mapped[list | None] = mapped_column(JSON)
    limitations: Mapped[list | None] = mapped_column(JSON)

    # Zähler für die UI
    queries_total: Mapped[int] = mapped_column(Integer, default=0)
    queries_done: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)

    queries: Mapped[list["SearchQuery"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SearchQuery(Base):
    """Eine tatsächlich ausgeführte Suchanfrage (Coverage-Nachweis)."""

    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"), index=True)

    platform: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))
    term: Mapped[str] = mapped_column(Text)
    family: Mapped[str | None] = mapped_column(String(32))
    # Suchansicht der Plattform (z. B. "newest" bei eBay). Leer = Standardsicht.
    sort: Mapped[str | None] = mapped_column(String(24))
    round_index: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    offset_reached: Mapped[int] = mapped_column(Integer, default=0)
    reported_total: Mapped[int | None] = mapped_column(Integer)
    results_returned: Mapped[int] = mapped_column(Integer, default=0)
    results_usable: Mapped[int] = mapped_column(Integer, default=0)
    excluded_auctions: Mapped[int] = mapped_column(Integer, default=0)
    excluded_unknown_format: Mapped[int] = mapped_column(Integer, default=0)
    excluded_price: Mapped[int] = mapped_column(Integer, default=0)
    # Treffer, deren Titel die Titelprüfung dem gesuchten Produkt nicht
    # zugeordnet hat. Solche Angebote werden nicht gespeichert.
    excluded_title_filter: Mapped[int] = mapped_column(Integer, default=0)
    new_listings: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[SearchRun] = relationship(back_populates="queries")


class AppSetting(Base):
    """Schluessel/Wert-Speicher für vom Benutzer editierbare Einstellungen."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
