"""Endpunkte für Systemzustand, Einstellungen, Wissensbasis und Benchmarks."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..benchmarks.deal_benchmark import run_benchmark
from ..categories import CATEGORIES
from ..config import (
    RuntimeSettings,
    ScoreWeights,
    Settings,
    redact,
)
from ..deps import AppState, get_session, get_state
from ..domain.price_limits import load_price_list, rewrite_price_limits
from ..models import Listing, SearchRun
from ..pricing.limit_advisor import advise_limits
from ..schemas import (
    BenchmarkOut,
    CategoryOut,
    ConnectorTestOut,
    KnowledgeEntryOut,
    KnowledgeSummaryOut,
    PriceLimitAdviceOut,
    PriceListOut,
    SettingsOut,
    SystemStatusOut,
)
from ..runtime_guard import status as runtime_status
from ..services.repository import count_active
from ..services.settings_store import reset_runtime_settings, save_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])

VERSION = "0.1.0"


@router.get("/health")
def health(state: AppState = Depends(get_state)) -> dict:
    """Zustand des laufenden Prozesses.

    ``runtime`` sagt, ob dieser Prozess noch mit dem Code vom Startzeitpunkt
    arbeitet oder ob seither Quelldateien geändert wurden. Die Portprüfung der
    Startskripte entscheidet daran, ob ein Neustart nötig ist statt "läuft ja
    schon" (siehe ``scripts/port_check.py``).
    """
    return {
        "status": "ok",
        "version": VERSION,
        "active_runs": state.manager.active_run_ids(),
        "runtime": runtime_status(),
    }


@router.get("/system/status", response_model=SystemStatusOut)
def system_status(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> SystemStatusOut:
    knowledge = state.knowledge.summary()
    runs_total = session.scalar(select(func.count(SearchRun.id))) or 0
    return SystemStatusOut(
        database={
            "url": state.settings.database_url,
            "search_runs": int(runs_total),
        },
        listings=count_active(session),
        knowledge=KnowledgeSummaryOut(
            entries=knowledge.entries,
            identity_only_entries=knowledge.identity_only_entries,
            priced_entries=knowledge.priced_entries,
            sources=knowledge.sources,
            per_category=knowledge.per_category,
            loaded_at=knowledge.loaded_at,
            paths=knowledge.paths,
            errors=knowledge.errors,
            markdown_notes=[
                {"name": name, "content": content[:4000]}
                for name, content in state.knowledge.markdown_notes()
            ],
        ),
        connectors=state.registry.report(),
        version=VERSION,
    )


@router.get("/system/categories", response_model=list[CategoryOut])
def list_categories() -> list[CategoryOut]:
    return [
        CategoryOut(
            key=category.key,
            label=category.label,
            default_max_price=category.default_max_price,
            base_terms=list(category.base_terms),
        )
        for category in CATEGORIES
    ]


@router.get("/connectors")
def list_connectors(state: AppState = Depends(get_state)) -> list[dict]:
    """Capability-Bericht je Plattform - inklusive ehrlicher Einschränkungen."""
    return state.registry.report()


@router.post("/connectors/{platform}/test", response_model=ConnectorTestOut)
async def test_connector(
    platform: str, state: AppState = Depends(get_state)
) -> ConnectorTestOut:
    """Plattform wirklich ausprobieren ("Testen"-Knopf).

    Bei eBay wird dafür eine echte Mini-Suche ausgeführt: Nur so ist belegt,
    dass Zugangsdaten, Scope, Marktplatz und Trefferauswertung funktionieren -
    ein Token allein sagt darüber nichts.
    """
    connector = state.registry.get(platform)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"Unbekannte Plattform '{platform}'.")
    try:
        ok, message, details = await connector.test_connection()
    except Exception as exc:  # Ein Test darf die Oberfläche nie mit 500 beenden
        logger.warning("Plattformtest für %s fehlgeschlagen: %s", platform, exc)
        return ConnectorTestOut(
            platform=platform,
            ok=False,
            message=f"Test fehlgeschlagen: {exc}",
            details={},
        )
    if ok:
        message = details.get("ergebnis") or f"{connector.capabilities.label} ist einsatzbereit."
    return ConnectorTestOut(
        platform=platform,
        ok=ok,
        message=message or "Unbekannter Fehler",
        details=details,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings_endpoint(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> SettingsOut:
    runtime = state.runtime(session)
    _usable, notices = state.registry.usable_platforms(
        [c["platform"] for c in state.registry.report()]
    )
    return SettingsOut(
        runtime=runtime,
        environment=redact(state.settings),
        connector_notices=notices,
    )


@router.put("/settings", response_model=SettingsOut)
def update_settings(
    payload: RuntimeSettings,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> SettingsOut:
    if payload.search_max_variants_per_category < 1:
        raise HTTPException(status_code=422, detail="Mindestens 1 Suchvariante erforderlich.")
    if payload.search_max_parallel_requests < 1:
        raise HTTPException(status_code=422, detail="Mindestens 1 paralleler Request erforderlich.")
    weights = payload.weights
    if sum(max(0.0, v) for v in weights.model_dump().values()) <= 0:
        raise HTTPException(
            status_code=422, detail="Mindestens ein Score-Gewicht muss größer als 0 sein."
        )
    save_runtime_settings(session, payload)
    _usable, notices = state.registry.usable_platforms(
        [c["platform"] for c in state.registry.report()]
    )
    return SettingsOut(runtime=payload, environment=redact(state.settings), connector_notices=notices)


@router.post("/settings/reset", response_model=SettingsOut)
def reset_settings(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> SettingsOut:
    runtime = reset_runtime_settings(session, state.settings)
    return SettingsOut(runtime=runtime, environment=redact(state.settings), connector_notices=[])


@router.put("/settings/weights", response_model=ScoreWeights)
def update_weights(
    payload: ScoreWeights,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> ScoreWeights:
    if sum(max(0.0, v) for v in payload.model_dump().values()) <= 0:
        raise HTTPException(
            status_code=422, detail="Mindestens ein Score-Gewicht muss größer als 0 sein."
        )
    runtime = state.runtime(session)
    runtime.weights = payload
    save_runtime_settings(session, runtime)
    return payload


@router.get("/knowledge", response_model=KnowledgeSummaryOut)
def knowledge_summary(state: AppState = Depends(get_state)) -> KnowledgeSummaryOut:
    summary = state.knowledge.summary()
    return KnowledgeSummaryOut(
        entries=summary.entries,
        identity_only_entries=summary.identity_only_entries,
        priced_entries=summary.priced_entries,
        sources=summary.sources,
        per_category=summary.per_category,
        loaded_at=summary.loaded_at,
        paths=summary.paths,
        errors=summary.errors,
        markdown_notes=[
            {"name": name, "content": content[:4000]}
            for name, content in state.knowledge.markdown_notes()
        ],
    )


@router.get("/knowledge/entries", response_model=list[KnowledgeEntryOut])
def knowledge_entries(
    state: AppState = Depends(get_state),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list:
    return state.knowledge.search(q, category)[:limit]


@router.post("/knowledge/reload", response_model=KnowledgeSummaryOut)
def knowledge_reload(state: AppState = Depends(get_state)) -> KnowledgeSummaryOut:
    summary = state.reload_knowledge()
    return KnowledgeSummaryOut(
        entries=summary.entries,
        identity_only_entries=summary.identity_only_entries,
        priced_entries=summary.priced_entries,
        sources=summary.sources,
        per_category=summary.per_category,
        loaded_at=summary.loaded_at,
        paths=summary.paths,
        errors=summary.errors,
        markdown_notes=[
            {"name": name, "content": content[:4000]}
            for name, content in state.knowledge.markdown_notes()
        ],
    )


@router.get("/price-limits", response_model=PriceListOut)
def price_limits(state: AppState = Depends(get_state)) -> PriceListOut:
    """Komponenten und Kauf-Limits aus der Liste ("Höchstpreise.txt").

    Die Liste wird bei jeder Abfrage neu eingelesen (Cache über Änderungszeit),
    damit Änderungen an der Datei sofort wirken.
    """
    return PriceListOut(**load_price_list(state.settings.price_list_path).to_dict())


@router.get("/price-limits/suggestions", response_model=PriceLimitAdviceOut)
def price_limit_suggestions(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> PriceLimitAdviceOut:
    """Schlägt Kauf-Limits aus den beobachteten Angebotspreisen vor.

    Grundlage sind ausschließlich die bereits gespeicherten, aktiven Angebote
    (günstigstes Zehntel je Komponente). Es wird nichts geschätzt: Bei zu dünner
    Datenlage bleibt eine Empfehlung aus und das Limit unverändert. Übernommen
    wird erst mit ``POST /api/price-limits/apply`` - nichts geschieht still.
    """
    runtime = state.runtime(session)
    report = advise_limits(
        session,
        load_price_list(state.settings.price_list_path),
        min_observations=runtime.market_min_observations,
        window_days=runtime.market_reference_window_days,
    )
    return PriceLimitAdviceOut(**report.to_dict())


@router.post("/price-limits/apply", response_model=PriceLimitAdviceOut)
def apply_price_limit_suggestions(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> PriceLimitAdviceOut:
    """Übernimmt die Vorschläge in ``Höchstpreise.txt``.

    Geschrieben werden nur echte Änderungen - Überschriften, Kommentare und
    Hinweise der Datei bleiben unverändert. Vorher entsteht eine Sicherungskopie
    im selben Ordner; ihr Pfad steht in der Antwort.
    """
    price_list = load_price_list(state.settings.price_list_path)
    if not price_list.available:
        raise HTTPException(
            status_code=422,
            detail=(
                "Es ist keine Kauf-Limit-Liste geladen - es gibt nichts "
                "nachzuführen. Pfad prüfen (PRICE_LIST_PATH)."
            ),
        )
    runtime = state.runtime(session)
    report = advise_limits(
        session,
        price_list,
        min_observations=runtime.market_min_observations,
        window_days=runtime.market_reference_window_days,
    )
    updates = report.actionable()
    if not updates:
        payload = report.to_dict()
        payload["summary"] = "Keine Änderung übernommen. " + report.summary
        return PriceLimitAdviceOut(**payload)
    try:
        applied, backup = rewrite_price_limits(price_list.path, updates)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Die Kauf-Limit-Liste konnte nicht geschrieben werden: {exc}",
        ) from exc
    payload = report.to_dict()
    payload["applied"] = applied
    payload["backup_path"] = backup or None
    payload["summary"] = (
        f"{applied} Kauf-Limit(s) in '{price_list.path}' nachgeführt. "
        f"Sicherung: {backup}."
    )
    logger.info("Kauf-Limits nachgeführt: %s (Sicherung %s)", applied, backup)
    return PriceLimitAdviceOut(**payload)


@router.post("/benchmark/deals", response_model=BenchmarkOut)
def deal_benchmark(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> BenchmarkOut:
    runtime = state.runtime(session)
    result = run_benchmark(runtime.weights)
    return BenchmarkOut(**result)


@router.post("/maintenance/clear")
async def clear_data(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> dict:
    """Leert Angebote und Suchverlauf (Neuanfang), Einstellungen bleiben.

    Laufende Suchen werden vorher abgebrochen **und abgewartet**: Ein noch
    laufender Suchlauf wuerde die eben geleerten Tabellen sofort wieder
    fuellen. Danach stehen Angebote und Verlauf bei null, die Wissensbasis und
    alle Einstellungen bleiben unberuehrt.
    """
    from ..db import clear_stored_data

    running = state.manager.active_run_ids()
    if running:
        await state.manager.shutdown()
    removed = clear_stored_data(session)
    message = (
        f"Geleert: {removed['listings']} Angebote, {removed['search_runs']} Suchläufe "
        f"und {removed['search_queries']} Suchanfragen entfernt. "
        "Einstellungen und Wissensbasis sind unverändert."
    )
    if running:
        message += f" {len(running)} laufende Suche(n) wurden dafür abgebrochen."
    logger.info("Gespeicherte Daten geleert: %s", removed)
    return {"cleared": removed, "cancelled_runs": running, "message": message}


@router.post("/maintenance/cleanup")
def cleanup(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> dict:
    from ..db import cleanup_old_records

    runtime = state.runtime(session)
    removed = cleanup_old_records(session, runtime.retention_days)
    return {
        "removed": removed,
        "retention_days": runtime.retention_days,
        "message": (
            f"Aufbewahrungsfrist {runtime.retention_days} Tage angewendet: "
            f"{removed['search_runs']} Suchläufe "
            f"und {removed['listings']} nicht mehr verfügbare Angebote entfernt."
        ),
    }


@router.get("/stats/overview")
def listing_stats(
    state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> dict:
    """Kennzahlen für die Kopfzeile der Oberfläche."""
    counts = count_active(session)
    best = session.scalars(
        select(Listing)
        .where(Listing.deal_score.is_not(None))
        .order_by(Listing.deal_score.desc())
        .limit(1)
    ).first()
    new_today = session.scalar(
        select(func.count(Listing.id)).where(
            Listing.fetched_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        )
    )
    return {
        "listings": counts,
        "best_score": best.deal_score if best else None,
        "best_listing_id": best.id if best else None,
        "new_today": int(new_today or 0),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def settings_for_tests() -> Settings:  # pragma: no cover - Hilfsfunktion
    from ..config import get_settings

    return get_settings()
