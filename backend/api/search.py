"""Endpunkte für Suchläufe: starten, beobachten, pausieren, abbrechen."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import AppState, get_session, get_state
from ..models import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    SearchQuery,
    SearchRun,
)
from ..schemas import (
    QueryOut,
    RunCreatedOut,
    RunOut,
    RunSummaryOut,
    SearchRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Suche"])

ACTIVE_STATUSES = {RUN_PENDING, RUN_RUNNING}


@router.post("/search/runs", response_model=RunCreatedOut, status_code=201)
async def start_search_run(
    request: SearchRequest,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> RunCreatedOut:
    """Startet einen Suchlauf."""
    active = state.manager.active_run_ids()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Es läuft bereits ein Suchlauf (Nr. {active[0]}). Bitte diesen "
                "zuerst beenden oder abbrechen, damit die Suchlimits eingehalten werden."
            ),
        )

    usable, notices = state.registry.usable_platforms(request.platforms)
    if not usable:
        details = "; ".join(str(n.get("message", "")) for n in notices)
        raise HTTPException(
            status_code=422,
            detail=(
                "Keine der ausgewählten Plattformen ist nutzbar. " + details
            ).strip(),
        )

    run_id = await state.manager.start(request)
    state.manager.prune_controls()
    warnings = [str(n.get("message", "")) for n in notices]
    message = "Suchlauf gestartet."
    if warnings:
        message += " Hinweise: " + " | ".join(warnings)
    return RunCreatedOut(run_id=run_id, status=RUN_PENDING, message=message)


@router.get("/search/runs", response_model=list[RunSummaryOut])
def list_search_runs(
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[SearchRun]:
    return list(
        session.scalars(select(SearchRun).order_by(SearchRun.id.desc()).limit(limit)).all()
    )


@router.get("/search/runs/{run_id}", response_model=RunOut)
def get_search_run(
    run_id: int,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
) -> SearchRun:
    run = session.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Suchlauf {run_id} nicht gefunden")
    control = state.manager.control(run_id)
    if control is not None and state.manager.is_active(run_id):
        # Live-Fortschritt hat Vorrang vor dem zuletzt gespeicherten Stand.
        live = {k: v for k, v in control.progress.items() if not k.startswith("_")}
        live["paused"] = control.paused
        live["cancelled"] = control.cancel.is_set()
        run.progress = live
        run.queries_done = int(live.get("queries_done", run.queries_done))
        run.queries_total = max(run.queries_total, int(live.get("queries_total", 0)))
        if run.status in ACTIVE_STATUSES and control.cancel.is_set():
            run.status = RUN_CANCELLED
    return run


@router.post("/search/runs/{run_id}/pause", response_model=RunCreatedOut)
def pause_search_run(
    run_id: int, state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> RunCreatedOut:
    if not state.manager.pause(run_id):
        raise HTTPException(status_code=409, detail="Dieser Suchlauf läuft nicht mehr.")
    run = session.get(SearchRun, run_id)
    if run is not None:
        run.status = "paused"
        progress = dict(run.progress or {})
        progress["paused"] = True
        run.progress = progress
        session.commit()
    return RunCreatedOut(run_id=run_id, status="paused", message="Suchlauf pausiert.")


@router.post("/search/runs/{run_id}/resume", response_model=RunCreatedOut)
def resume_search_run(
    run_id: int, state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> RunCreatedOut:
    if not state.manager.resume(run_id):
        raise HTTPException(status_code=409, detail="Dieser Suchlauf läuft nicht mehr.")
    run = session.get(SearchRun, run_id)
    if run is not None:
        run.status = RUN_RUNNING
        progress = dict(run.progress or {})
        progress["paused"] = False
        run.progress = progress
        session.commit()
    return RunCreatedOut(run_id=run_id, status=RUN_RUNNING, message="Suchlauf wird fortgesetzt.")


@router.post("/search/runs/{run_id}/cancel", response_model=RunCreatedOut)
def cancel_search_run(
    run_id: int, state: AppState = Depends(get_state), session: Session = Depends(get_session)
) -> RunCreatedOut:
    if not state.manager.cancel(run_id):
        raise HTTPException(
            status_code=409,
            detail="Dieser Suchlauf ist bereits beendet und kann nicht abgebrochen werden.",
        )
    return RunCreatedOut(
        run_id=run_id, status=RUN_CANCELLED, message="Abbruch wurde angefordert."
    )


@router.get("/search/runs/{run_id}/queries", response_model=list[QueryOut])
def list_run_queries(
    run_id: int,
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> list[SearchQuery]:
    stmt = select(SearchQuery).where(SearchQuery.run_id == run_id)
    if status:
        stmt = stmt.where(SearchQuery.status == status)
    if platform:
        stmt = stmt.where(SearchQuery.platform == platform)
    return list(
        session.scalars(stmt.order_by(SearchQuery.id.asc()).limit(limit)).all()
    )


@router.get("/search/runs/{run_id}/coverage")
def get_run_coverage(
    run_id: int, session: Session = Depends(get_session)
) -> dict:
    run = session.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Suchlauf {run_id} nicht gefunden")
    if run.coverage:
        return run.coverage
    # Noch kein Bericht gespeichert: aus den Anfragen ableiten.
    from ..search.coverage import build_coverage

    queries = list(
        session.scalars(select(SearchQuery).where(SearchQuery.run_id == run_id)).all()
    )
    return build_coverage(run, queries)


@router.get("/search/runs/{run_id}/summary")
def get_run_summary(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = session.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Suchlauf {run_id} nicht gefunden")
    by_platform = session.execute(
        select(SearchQuery.platform, func.count(SearchQuery.id))
        .where(SearchQuery.run_id == run_id)
        .group_by(SearchQuery.platform)
    ).all()
    by_family = session.execute(
        select(SearchQuery.family, func.count(SearchQuery.id))
        .where(SearchQuery.run_id == run_id)
        .group_by(SearchQuery.family)
    ).all()
    return {
        "run_id": run.id,
        "status": run.status,
        "plan_source": run.plan_source,
        "queries": {str(p): int(c) for p, c in by_platform},
        "families": {str(f or "unbekannt"): int(c) for f, c in by_family},
        "plan": run.plan,
        "errors": run.errors,
        "limitations": run.limitations,
    }
