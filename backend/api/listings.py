"""Endpunkte für Angebote: Liste, Filter, Details, Favoriten, Erklärung."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..deps import AppState, get_session, get_state
from ..domain.normalize import CONDITION_LABELS
from ..domain.price_limits import load_price_list
from ..models import FORMAT_FIXED_PRICE, Listing, SearchRun
from ..search.revalidate import revalidate_stored_listings
from ..schemas import ExplainOut, ListingDetailOut, ListingListOut, ListingOut, PricePoint
from ..services.repository import (
    ListingQuery,
    get_listing,
    price_history_for,
    query_listings,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Angebote"])


def _to_listing_out(listing: Listing) -> ListingOut:
    breakdown = listing.score_breakdown or {}
    model = ListingOut.model_validate(listing)
    model.price_advantage_abs = breakdown.get("price_advantage_abs")
    model.price_advantage_pct = breakdown.get("price_advantage_pct")
    basis = listing.market_basis or breakdown.get("market_basis") or {}
    model.market_uncertain = bool(basis.get("uncertain"))
    model.market_method = basis.get("method")
    return model


@router.get("/listings", response_model=ListingListOut)
def list_listings(
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
    platform: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    model: list[str] | None = Query(default=None),
    verdict: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    min_score: float | None = Query(default=None, ge=0, le=100),
    max_price: float | None = Query(default=None, ge=0),
    min_price: float | None = Query(default=None, ge=0),
    min_condition_rank: int | None = Query(default=None, ge=0, le=4),
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    favorites: bool = Query(default=False),
    fixed_price_only: bool = Query(default=True),
    delivery: str | None = Query(default=None, pattern="^(shipping|pickup)$"),
    search: str | None = Query(default=None),
    new_since_run: int | None = Query(default=None),
    sort: str = Query(
        default="best_deal",
        pattern="^(best_deal|price_advantage|lowest_price|price_performance|newest|confidence)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ListingListOut:
    query = ListingQuery(
        platforms=platform or [],
        categories=category or [],
        models=model or [],
        verdicts=verdict or [],
        statuses=status or [],
        min_score=min_score,
        max_price=max_price,
        min_price=min_price,
        min_condition_rank=min_condition_rank,
        min_confidence=min_confidence,
        only_favorites=favorites,
        fixed_price_only=fixed_price_only,
        delivery=delivery,
        search_text=search,
        new_since_run=new_since_run,
        sort=sort,
        limit=limit,
        offset=offset,
    )

    def performance(listing: Listing) -> float | None:
        if not listing.category or not listing.model:
            return None
        entry = state.knowledge.find_entry(listing.category, listing.model)
        return entry.performance_index if entry else None

    # Kauf-Limits: Was über dem Limit seiner Komponente liegt, wird gar nicht
    # erst angezeigt ("Alles darüber lohnt sich nicht").
    price_list = load_price_list(state.settings.price_list_path)

    def within_price_limit(listing: Listing) -> bool:
        return price_list.allows(
            title=listing.title,
            price=listing.total_price if listing.total_price is not None else listing.price,
            model=listing.model,
            variant=listing.variant,
        )

    items, total = query_listings(
        session,
        query,
        performance=performance,
        keep=within_price_limit if price_list.available else None,
    )
    return ListingListOut(
        items=[_to_listing_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
        sort=sort,
    )


@router.get("/listings/filters")
def listing_filter_options(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> dict:
    """Verfügbare Filterwerte aus echten Daten (keine erfundenen Listen)."""
    from sqlalchemy import func, select

    from ..models import Listing as ListingModel

    platforms = session.execute(
        select(ListingModel.platform, func.count(ListingModel.id))
        .group_by(ListingModel.platform)
        .order_by(func.count(ListingModel.id).desc())
    ).all()
    models = session.execute(
        select(ListingModel.category, ListingModel.model, func.count(ListingModel.id))
        .where(ListingModel.model.is_not(None))
        .group_by(ListingModel.category, ListingModel.model)
        .order_by(func.count(ListingModel.id).desc())
        .limit(200)
    ).all()
    verdicts = session.execute(
        select(ListingModel.verdict, func.count(ListingModel.id))
        .where(ListingModel.verdict.is_not(None))
        .group_by(ListingModel.verdict)
    ).all()
    runs = session.execute(
        select(SearchRun.id, SearchRun.created_at).order_by(SearchRun.id.desc()).limit(20)
    ).all()
    return {
        "platforms": [{"value": platform, "count": int(count)} for platform, count in platforms],
        "models": [
            {"category": category, "model": model, "count": int(count)}
            for category, model, count in models
        ],
        "verdicts": [{"value": verdict, "count": int(count)} for verdict, count in verdicts],
        "conditions": [
            {"rank": rank, "label": label} for rank, label in sorted(CONDITION_LABELS.items())
        ],
        "runs": [
            {"id": run_id, "created_at": created.isoformat() if created else None}
            for run_id, created in runs
        ],
        "knowledge_categories": sorted(state.knowledge.summary().per_category.keys()),
    }


@router.post("/listings/revalidate")
def revalidate_listings(
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
    platform: str | None = Query(default=None),
) -> dict:
    """Prüft den gespeicherten Bestand erneut und räumt ihn auf.

    Die Titelprüfung wird mit jeder Verbesserung strenger; alte Angebote, die
    eine frühere Fassung durchgelassen hat, bleiben sonst für immer in der
    Ergebnisliste stehen. Hier wird der Bestand mit den aktuellen Regeln neu
    beurteilt - Aussortiertes verschwindet aus der Liste, der Datensatz bleibt
    mit Begründung erhalten. Ein Suchlauf macht dasselbe automatisch am Anfang.
    """
    price_list = load_price_list(state.settings.price_list_path)
    report = revalidate_stored_listings(session, price_list=price_list, platform=platform)
    return {"ok": True, "summary": report.summary(), **report.to_dict()}


@router.get("/listings/{listing_id}", response_model=ListingDetailOut)
def get_listing_detail(
    listing_id: int, session: Session = Depends(get_session)
) -> ListingDetailOut:
    listing = get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Angebot {listing_id} nicht gefunden")
    history = price_history_for(session, listing_id)
    detail = ListingDetailOut.model_validate(listing)
    basis = listing.market_basis or (listing.score_breakdown or {}).get("market_basis") or {}
    breakdown = listing.score_breakdown or {}
    detail.price_advantage_abs = breakdown.get("price_advantage_abs")
    detail.price_advantage_pct = breakdown.get("price_advantage_pct")
    detail.market_uncertain = bool(basis.get("uncertain"))
    detail.market_method = basis.get("method")
    detail.price_history = [
        PricePoint(
            observed_at=entry.observed_at,
            price=entry.price,
            shipping_cost=entry.shipping_cost,
            total_price=entry.total_price,
            status=entry.status,
        )
        for entry in history
    ]
    return detail


@router.post("/listings/{listing_id}/favorite", response_model=ListingOut)
def set_favorite(
    listing_id: int,
    favorite: bool = Query(default=True),
    note: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> ListingOut:
    listing = get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Angebot {listing_id} nicht gefunden")
    listing.is_favorite = favorite
    if note is not None:
        listing.favorite_note = note
    session.commit()
    return _to_listing_out(listing)


@router.post("/listings/{listing_id}/recheck")
async def recheck_listing(
    listing_id: int,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> dict:
    """Fragt die Quelle erneut ab, um festzustellen, ob das Angebot noch existiert.

    Nur wenn die Plattform das eindeutig meldet, wird der Status geändert.
    """
    listing = get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Angebot {listing_id} nicht gefunden")
    connector = state.registry.get(listing.platform)
    if connector is None or not connector.capabilities.supports_details:
        return {
            "checked": False,
            "message": (
                f"Für {listing.platform} ist kein Detailabruf verfügbar - die "
                "Verfügbarkeit kann nicht überprüft werden."
            ),
            "status": listing.status,
        }
    if not listing.platform_listing_id:
        return {
            "checked": False,
            "message": "Keine Plattform-ID vorhanden - Verfügbarkeit nicht prüfbar.",
            "status": listing.status,
        }
    try:
        detail = await connector.fetch_details(listing.platform_listing_id)
    except Exception as exc:
        return {"checked": False, "message": f"Prüfung fehlgeschlagen: {exc}", "status": listing.status}
    if detail is None:
        listing.status = "gone"
        session.commit()
        return {
            "checked": True,
            "message": "Die Quelle meldet das Angebot nicht mehr - als nicht verfügbar markiert.",
            "status": listing.status,
        }
    return {
        "checked": True,
        "message": "Angebot ist weiterhin verfügbar.",
        "status": listing.status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _deterministic_explanation(listing: Listing, message: str) -> ExplainOut:
    """Erklärung aus den bereits berechneten, nachvollziehbaren Werten.

    Der Deal-Score entsteht deterministisch; die Begründung ebenso. Jede Zahl
    lässt sich im Detail nachrechnen.
    """
    breakdown = listing.score_breakdown or {}
    verdict = breakdown.get("verdict_label") or "Unbewertet"
    reasons = [str(item) for item in (listing.reasons or [])]
    warnings = [str(item) for item in (listing.warnings or [])]

    parts = [verdict]
    if listing.deal_score is not None:
        parts.append(f"Score {listing.deal_score:.0f} von 100")
    if listing.total_price is not None:
        parts.append(f"Gesamtpreis {listing.total_price:.2f} {listing.currency or 'EUR'}")
    if listing.market_value is not None:
        parts.append(f"Marktwert-Schätzung {listing.market_value:.2f} EUR")
    summary = " | ".join(parts)
    if reasons:
        summary = f"{summary}. {reasons[0]}"
    return ExplainOut(
        available=False,
        source="deterministic",
        message=message,
        summary=summary,
        main_arguments=reasons[:5],
        concerns=warnings[:5],
    )


@router.post("/listings/{listing_id}/explain", response_model=ExplainOut)
def explain_listing(
    listing_id: int,
    session: Session = Depends(get_session),
) -> ExplainOut:
    """Erklärt den bestehenden Bewertungs-Score nachvollziehbar.

    Der Score selbst wird dabei nicht verändert.
    """
    listing = get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Angebot {listing_id} nicht gefunden")
    if listing.score_breakdown is None:
        return ExplainOut(
            available=False,
            source="unavailable",
            message="Für dieses Angebot liegt noch keine Bewertung vor.",
        )
    return _deterministic_explanation(
        listing,
        "Angezeigt wird die nachvollziehbare deterministische Bewertung.",
    )


@router.get("/listings/{listing_id}/market")
def listing_market(
    listing_id: int,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> dict:
    """Marktwertnachweis inklusive Datenbasis und Unsicherheit."""
    listing = get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail=f"Angebot {listing_id} nicht gefunden")
    basis = listing.market_basis or (listing.score_breakdown or {}).get("market_basis") or {}
    return {
        "listing_id": listing.id,
        "estimated_value": listing.market_value,
        "confidence": listing.confidence,
        "basis": basis,
        "comparables_possible": bool(
            listing.category and listing.model
        ),
        "model": listing.model,
        "category": listing.category,
        "disclaimer": (
            "Beobachtete Angebotspreise sind keine erzielten Verkaufspreise. "
            "Ein manuell gepflegter Richtwert aus der Wissensbasis ist eine "
            "separate, klar gekennzeichnete Quelle."
        ),
    }
