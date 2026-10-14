"""Datenzugriff für Angebote (Abschnitt 8).

* Deduplizierung primaer über (Plattform, Plattform-Listing-ID), dann über
  die kanonische URL, dann über einen inhaltlichen Fingerabdruck.
* Preisänderungen werden in einer eigenen Historie festgehalten.
* Es wird nie das gesamte Archiv geladen: Abfragen sind gefiltert, begrenzt
  und indexgestützt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..domain.dedup import canonical_url, fingerprint
from ..models import (
    STATUS_ACTIVE,
    STATUS_GONE,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    Listing,
    PriceHistory,
    utcnow,
)

logger = logging.getLogger(__name__)

OUTCOME_NEW = "new"
OUTCOME_UPDATED = "updated"
OUTCOME_UNCHANGED = "unchanged"

# Obergrenze für Sortierungen, die in Python berechnet werden muessen.
MAX_SORT_CANDIDATES = 5000


@dataclass
class ListingPayload:
    """Alle speicherrelevanten Felder eines Angebots."""

    platform: str
    url: str
    title: str
    platform_listing_id: str | None = None
    canonical_url: str | None = None
    dedup_fingerprint: str | None = None
    description: str | None = None

    price: float | None = None
    shipping_cost: float | None = None
    shipping_cost_known: bool = False
    total_price: float | None = None
    currency: str = "EUR"

    published_at: datetime | None = None
    published_at_known: bool = False
    fetched_at: datetime | None = None

    seller_name: str | None = None
    seller_feedback_percent: float | None = None
    seller_feedback_score: int | None = None
    location_text: str | None = None
    postal_code: str | None = None
    country: str | None = None

    condition_text: str | None = None
    condition_rank: int | None = None

    category: str | None = None
    manufacturer: str | None = None
    product_family: str | None = None
    model: str | None = None
    variant: str | None = None
    model_candidates: list | None = None

    listing_format: str = "unknown"
    format_evidence: str | None = None
    pickup_available: bool = False
    status: str = STATUS_ACTIVE

    image_url: str | None = None
    raw: dict | None = None


@dataclass
class ListingQuery:
    """Filter und Sortierung für die Ergebnisliste."""

    platforms: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    min_score: float | None = None
    max_price: float | None = None
    min_price: float | None = None
    min_condition_rank: int | None = None
    min_confidence: float | None = None
    only_favorites: bool = False
    fixed_price_only: bool = False
    delivery: str | None = None  # "shipping" | "pickup" | None
    search_text: str | None = None
    new_since_run: int | None = None
    sort: str = "best_deal"
    limit: int = 50
    offset: int = 0


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Deduplizierung
# ---------------------------------------------------------------------------


def build_dedup_fields(
    *,
    platform: str,
    url: str,
    title: str,
    total_price: float | None,
    seller_name: str | None,
) -> tuple[str, str]:
    return canonical_url(url), fingerprint(platform, title, total_price, seller_name)


def find_duplicate(session: Session, payload: ListingPayload) -> Listing | None:
    """Sucht ein bereits gespeichertes Angebot (3 Stufen)."""
    if payload.platform_listing_id:
        found = session.scalars(
            select(Listing).where(
                Listing.platform == payload.platform,
                Listing.platform_listing_id == payload.platform_listing_id,
            )
        ).first()
        if found is not None:
            return found

    canonical = payload.canonical_url or canonical_url(payload.url)
    if canonical:
        found = session.scalars(
            select(Listing).where(
                Listing.platform == payload.platform, Listing.canonical_url == canonical
            )
        ).first()
        if found is not None:
            return found

    if payload.dedup_fingerprint:
        candidates = session.scalars(
            select(Listing).where(
                Listing.platform == payload.platform,
                Listing.dedup_fingerprint == payload.dedup_fingerprint,
            )
        ).all()
        for candidate in candidates:
            # Der Fingerabdruck ist die schwaechste Stufe. Haben beide Seiten
            # eine Plattform-ID und unterscheiden die sich, sind es definitiv
            # zwei verschiedene Anzeigen - dann darf nicht zusammengeführt
            # werden, auch wenn Titel, Preis und Verkäufer gleich lauten.
            if (
                payload.platform_listing_id
                and candidate.platform_listing_id
                and candidate.platform_listing_id != payload.platform_listing_id
            ):
                continue
            return candidate
    return None


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

# Felder, deren Änderung einen erneuten Score rechtfertigt.
_SCORING_RELEVANT = (
    "total_price",
    "price",
    "shipping_cost",
    "condition_rank",
    "model",
    "variant",
    "title",
    "description",
    "category",
    "status",
)


def upsert_listing(
    session: Session,
    payload: ListingPayload,
    *,
    run_id: int | None = None,
    content_hash: str | None = None,
) -> tuple[Listing, str]:
    """Legt ein Angebot an oder aktualisiert es. Erkennt Preisänderungen."""
    existing = find_duplicate(session, payload)
    now = utcnow()

    if existing is None:
        listing = Listing(
            platform=payload.platform,
            platform_listing_id=payload.platform_listing_id,
            url=payload.url,
            canonical_url=payload.canonical_url or canonical_url(payload.url),
            dedup_fingerprint=payload.dedup_fingerprint,
            title=payload.title,
            description=payload.description,
            price=payload.price,
            shipping_cost=payload.shipping_cost,
            shipping_cost_known=payload.shipping_cost_known,
            total_price=payload.total_price,
            currency=payload.currency,
            published_at=payload.published_at,
            published_at_known=payload.published_at_known,
            fetched_at=payload.fetched_at or now,
            last_seen_at=now,
            seller_name=payload.seller_name,
            seller_feedback_percent=payload.seller_feedback_percent,
            seller_feedback_score=payload.seller_feedback_score,
            location_text=payload.location_text,
            postal_code=payload.postal_code,
            country=payload.country,
            condition_text=payload.condition_text,
            condition_rank=payload.condition_rank,
            category=payload.category,
            manufacturer=payload.manufacturer,
            product_family=payload.product_family,
            model=payload.model,
            variant=payload.variant,
            model_candidates=payload.model_candidates,
            listing_format=payload.listing_format,
            format_evidence=payload.format_evidence,
            pickup_available=payload.pickup_available,
            status=payload.status,
            image_url=payload.image_url,
            content_hash=content_hash,
            raw=payload.raw,
            first_seen_run_id=run_id,
            last_seen_run_id=run_id,
        )
        session.add(listing)
        session.flush()
        _record_history(session, listing, now)
        return listing, OUTCOME_NEW

    changes: dict[str, tuple[Any, Any]] = {}
    for field_name in (
        "title",
        "description",
        "price",
        "shipping_cost",
        "shipping_cost_known",
        "total_price",
        "condition_text",
        "condition_rank",
        "model",
        "variant",
        "category",
        "manufacturer",
        "product_family",
        "listing_format",
        "pickup_available",
        "status",
        "image_url",
        "seller_name",
        "postal_code",
        "location_text",
        "published_at",
    ):
        new_value = getattr(payload, field_name, None)
        if new_value in (None, ""):
            continue
        old_value = getattr(existing, field_name, None)
        if old_value != new_value:
            changes[field_name] = (old_value, new_value)
            setattr(existing, field_name, new_value)

    price_changed = any(
        key in changes for key in ("price", "shipping_cost", "total_price")
    )

    existing.last_seen_at = now
    existing.fetched_at = payload.fetched_at or now
    if run_id is not None:
        existing.last_seen_run_id = run_id
        if existing.first_seen_run_id is None:
            existing.first_seen_run_id = run_id
    if payload.platform_listing_id and not existing.platform_listing_id:
        existing.platform_listing_id = payload.platform_listing_id
    if payload.canonical_url and not existing.canonical_url:
        existing.canonical_url = payload.canonical_url
    if payload.dedup_fingerprint and not existing.dedup_fingerprint:
        existing.dedup_fingerprint = payload.dedup_fingerprint
    if payload.model_candidates:
        existing.model_candidates = payload.model_candidates
    if payload.raw:
        existing.raw = payload.raw
    existing.status = payload.status or existing.status

    if price_changed:
        _record_history(session, existing, now)

    scoring_relevant = [key for key in changes if key in _SCORING_RELEVANT]
    if scoring_relevant:
        existing.scored_at = None  # muss neu bewertet werden
        return existing, OUTCOME_UPDATED

    new_hash = content_hash
    if new_hash and existing.content_hash != new_hash:
        existing.content_hash = new_hash
        existing.scored_at = None
        return existing, OUTCOME_UPDATED
    return existing, OUTCOME_UNCHANGED


def _record_history(session: Session, listing: Listing, observed_at: datetime) -> None:
    session.add(
        PriceHistory(
            listing_id=listing.id,
            observed_at=observed_at,
            price=listing.price,
            shipping_cost=listing.shipping_cost,
            total_price=listing.total_price,
            status=listing.status,
        )
    )


def update_assessment(session: Session, listing: Listing, breakdown: dict) -> None:
    listing.deal_score = breakdown.get("score")
    listing.market_value = breakdown.get("market_value")
    listing.confidence = breakdown.get("confidence")
    listing.verdict = breakdown.get("verdict")
    listing.score_breakdown = breakdown
    listing.reasons = breakdown.get("reasons")
    listing.warnings = breakdown.get("warnings")
    listing.scored_at = utcnow()


def price_history_for(session: Session, listing_id: int) -> list[PriceHistory]:
    return list(
        session.scalars(
            select(PriceHistory)
            .where(PriceHistory.listing_id == listing_id)
            .order_by(PriceHistory.observed_at.asc())
        ).all()
    )


def mark_status(session: Session, listing: Listing, status: str, *, note: str | None = None) -> None:
    if listing.status != status:
        listing.status = status
        if note:
            warnings = list(listing.warnings or [])
            warnings.append(note)
            listing.warnings = warnings
        _record_history(session, listing, utcnow())


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


def _apply_filters(stmt, query: ListingQuery):
    if query.platforms:
        stmt = stmt.where(Listing.platform.in_(query.platforms))
    if query.categories:
        stmt = stmt.where(Listing.category.in_(query.categories))
    if query.models:
        stmt = stmt.where(Listing.model.in_(query.models))
    if query.verdicts:
        stmt = stmt.where(Listing.verdict.in_(query.verdicts))
    if query.statuses:
        stmt = stmt.where(Listing.status.in_(query.statuses))
    else:
        # Ohne ausdrücklichen Wunsch erscheinen nur verfügbare Angebote. Vom
        # Bestand aussortierte (``rejected``) und nicht mehr abrufbare
        # (``gone``) gehören nicht in die Ergebnisliste.
        stmt = stmt.where(Listing.status.in_([STATUS_ACTIVE, STATUS_UNKNOWN]))
    if query.min_score is not None:
        stmt = stmt.where(Listing.deal_score.is_not(None), Listing.deal_score >= query.min_score)
    if query.max_price is not None:
        stmt = stmt.where(Listing.total_price.is_not(None), Listing.total_price <= query.max_price)
    if query.min_price is not None:
        stmt = stmt.where(Listing.total_price.is_not(None), Listing.total_price >= query.min_price)
    if query.min_condition_rank is not None:
        stmt = stmt.where(
            Listing.condition_rank.is_not(None), Listing.condition_rank >= query.min_condition_rank
        )
    if query.min_confidence is not None:
        stmt = stmt.where(
            Listing.confidence.is_not(None), Listing.confidence >= query.min_confidence
        )
    if query.only_favorites:
        stmt = stmt.where(Listing.is_favorite.is_(True))
    if query.fixed_price_only:
        stmt = stmt.where(Listing.listing_format == "fixed_price")
    if query.new_since_run is not None:
        stmt = stmt.where(
            or_(
                Listing.last_seen_run_id == query.new_since_run,
                Listing.first_seen_run_id == query.new_since_run,
            )
        )
    # Der Lieferungsfilter steht bewusst nicht hier: Er braucht den Rohbeleg des
    # Angebots (die Versand-Angabe der Quelle) und lässt sich nicht in SQL
    # ausdrücken. Er wird in ``query_listings`` über ``_delivery_predicate``
    # angewendet - siehe ``is_pickup_only``.
    if query.search_text:
        needle = f"%{query.search_text.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Listing.title).like(needle),
                func.lower(Listing.model).like(needle),
                func.lower(Listing.manufacturer).like(needle),
            )
        )
    return stmt


def _sort_key(
    sort: str,
    performance: Callable[[Listing], float | None] | None,
) -> Callable[[Listing], tuple]:
    def numeric(value: float | None, default: float) -> float:
        return default if value is None else float(value)

    if sort == "price_advantage":
        return lambda item: (
            -numeric(
                (item.market_value - item.total_price)
                if (item.market_value is not None and item.total_price is not None)
                else None,
                default=-999.0,
            ),
        )
    if sort == "lowest_price":
        return lambda item: (numeric(item.total_price, default=float("inf")),)
    if sort == "newest":
        return lambda item: (
            -( _utc(item.published_at) or _utc(item.fetched_at) or datetime.min.replace(
                tzinfo=timezone.utc
            ) ).timestamp(),
        )
    if sort == "confidence":
        return lambda item: (-numeric(item.confidence, default=-1.0),)
    if sort == "price_performance":
        def key(item: Listing) -> tuple:
            perf = performance(item) if performance else None
            if perf is None or not item.total_price:
                return (1, 0.0)
            return (0, -perf / item.total_price)

        return key
    # Standard: bester Deal
    return lambda item: (-numeric(item.deal_score, default=-1.0),)


# ---------------------------------------------------------------------------
# Lieferung: Versand oder Abholung (Filter der Ergebnisliste)
# ---------------------------------------------------------------------------

# Belegte Nur-Abhol-Angebote aus dem Anzeigentext. Nur eindeutige Formulierungen:
# "nur Abholung", "kein Versand" - aber nicht "kein Versand ins Ausland".
_PICKUP_ONLY_TEXT = (
    re.compile(r"\bnur\s+(?:selbst)?abholung\b", re.IGNORECASE),
    re.compile(
        r"\bkein(?:e|er)?\s+versand\b(?!\s+(?:ins\b|in\s+di[en]\b|nach\b|au(?:ß|ss)erhalb\b))",
        re.IGNORECASE,
    ),
    re.compile(r"\bversand\s+(?:ist\s+)?nicht\s+m(?:ö|oe)glich\b", re.IGNORECASE),
    re.compile(r"\bnicht\s+versendbar\b", re.IGNORECASE),
)


def is_pickup_only(listing: Listing) -> bool:
    """Steht das Angebot belegt nur zur Abholung?

    Alle Belege stammen aus dem, was die Quelle selbst gesagt hat - es wird
    nichts geschätzt und nichts aus dem Preis abgeleitet:

    * Der Connector meldet Abholung bei unbekannten Versandkosten (eBay: keine
      Versandoption gemeldet; Kleinanzeigen: "Nur Abholung"). Genau dieselbe
      Regel verwirft auch die Ingestion bei einer reinen Versandsuche
      (siehe ``search.ingest``).
    * Kleinanzeigen ordnet jedes Angebot in der Trefferliste selbst ein:
      "Versand möglich" steht dort als Chip - fehlt er, führt die Plattform das
      Angebot unter "Nur Abholung". Live nachgeprüft: der Versandfilter der
      Plattform liefert 27 von 27 Angeboten mit Chip, der Abholfilter 0 von 27
      (Websuche "grafikkarte", 14.581 bzw. 3.958 Treffer). Die Einordnung steht
      im Rohbeleg des Angebots.
    * Der Anzeigentext sagt es ausdrücklich ("Nur Abholung", "Kein Versand").

    Der Filter ist eine Anzeigeeinstellung: Er blendet aus, er löscht nicht.
    "Versand und Abholung" zeigt dieselben Angebote weiterhin.
    """
    if listing.pickup_available and not listing.shipping_cost_known:
        return True
    raw = listing.raw if isinstance(listing.raw, dict) else {}
    extra = raw.get("extra")
    if isinstance(extra, dict) and extra.get("shipping_possible") is False:
        return True
    text = f"{listing.title or ''} {listing.description or ''}"
    return any(pattern.search(text) for pattern in _PICKUP_ONLY_TEXT)


def _delivery_predicate(delivery: str | None) -> Callable[[Listing], bool] | None:
    """Filter der Ergebnisliste für die gewählte Lieferungsart.

    ``None`` heißt: keine Einschränkung ("Versand und Abholung").
    """
    if delivery == "shipping":
        # Reine Versandsuche: Was belegt nur zur Abholung steht, gehört nicht in
        # die Liste - sonst stünden genau die Angebote darin, die man nicht
        # bestellen kann.
        return lambda listing: not is_pickup_only(listing)
    if delivery == "pickup":
        # Abholung braucht einen Ort: Ohne verwertbaren Verkäuferstandort ist
        # nicht klar, wohin die Fahrt ginge.
        return lambda listing: listing.postal_code is not None
    return None


def _combine(*predicates: Callable[[Listing], bool] | None) -> Callable[[Listing], bool] | None:
    """Mehrere Bedingungen zu einer zusammenfassen (``None`` = keine Bedingung)."""
    active = [predicate for predicate in predicates if predicate is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return lambda listing: all(predicate(listing) for predicate in active)


def query_listings(
    session: Session,
    query: ListingQuery,
    *,
    performance: Callable[[Listing], float | None] | None = None,
    keep: Callable[[Listing], bool] | None = None,
) -> tuple[list[Listing], int]:
    """Gefilterte Angebotsliste samt Gesamtzahl der angezeigten Angebote.

    ``keep`` entfernt Angebote, deren Ausschluss sich nicht in SQL ausdrücken
    lässt (Kauf-Limits je Modell, Lieferungsart). Die Gesamtzahl zählt dann nur
    noch das, was auch tatsächlich angezeigt wird - sonst würde die Blätterung
    lügen.
    """
    stmt = _apply_filters(select(Listing), query)
    rows = list(
        session.scalars(stmt.order_by(Listing.id.desc()).limit(MAX_SORT_CANDIDATES)).all()
    )
    delivery_ok = _delivery_predicate(query.delivery)
    keep = _combine(keep, delivery_ok)
    if keep is not None:
        rows = [row for row in rows if keep(row)]
        total = len(rows)
    else:
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if not rows:
        return [], total

    rows.sort(key=_sort_key(query.sort, performance))
    offset = max(0, query.offset)
    limit = max(1, min(query.limit, 500))
    return rows[offset : offset + limit], total


def get_listing(session: Session, listing_id: int) -> Listing | None:
    return session.get(Listing, listing_id)


def distinct_models(session: Session, *, limit: int = 300) -> list[tuple[str, str, int]]:
    rows = session.execute(
        select(Listing.category, Listing.model, func.count(Listing.id))
        .where(Listing.model.is_not(None))
        .group_by(Listing.category, Listing.model)
        .order_by(func.count(Listing.id).desc())
        .limit(limit)
    ).all()
    return [(str(category or ""), str(model), int(count)) for category, model, count in rows]


def comparable_listings(
    session: Session,
    *,
    category: str,
    model: str,
    exclude_id: int | None = None,
    limit: int = 50,
) -> list[Listing]:
    stmt = select(Listing).where(
        Listing.category == category,
        Listing.model == model,
        Listing.listing_format == "fixed_price",
        Listing.total_price.is_not(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Listing.id != exclude_id)
    return list(session.scalars(stmt.order_by(Listing.last_seen_at.desc()).limit(limit)).all())


def count_active(session: Session) -> dict[str, int]:
    total = session.scalar(select(func.count(Listing.id))) or 0
    active = (
        session.scalar(
            select(func.count(Listing.id)).where(Listing.status == STATUS_ACTIVE)
        )
        or 0
    )
    gone = (
        session.scalar(select(func.count(Listing.id)).where(Listing.status == STATUS_GONE)) or 0
    )
    unknown = (
        session.scalar(
            select(func.count(Listing.id)).where(Listing.status == STATUS_UNKNOWN)
        )
        or 0
    )
    # Vom Bestand aussortierte Angebote (Titelprüfung) bleiben als Datensatz
    # erhalten - die Gesamtzahl enthält sie, die Ergebnisliste nicht.
    rejected = (
        session.scalar(
            select(func.count(Listing.id)).where(Listing.status == STATUS_REJECTED)
        )
        or 0
    )
    return {
        "total": int(total),
        "active": int(active),
        "gone": int(gone),
        "unknown": int(unknown),
        "rejected": int(rejected),
    }
