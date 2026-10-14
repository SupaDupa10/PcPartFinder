"""Verwaltung laufender Suchaufträge.

Hält pro Suchlauf einen :class:`RunControl` (Pause/Abbrechen) und den
Hintergrund-Task. Fortschritt wird zusätzlich in der Datenbank gespeichert,
damit er auch nach einem Neustart sichtbar bleibt.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..connectors.registry import ConnectorRegistry
from ..knowledge.base import KnowledgeBase
from ..models import RUN_COMPLETED, RUN_FAILED, RUN_PENDING, RUN_RUNNING, SearchRun
from ..schemas import SearchRequest
from ..services.settings_store import load_runtime_settings
from .runner import RunControl, SearchRunner

logger = logging.getLogger(__name__)


class SearchRunManager:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ConnectorRegistry,
        knowledge: KnowledgeBase,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.knowledge = knowledge
        self.session_factory = session_factory
        self._controls: dict[int, RunControl] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    # -- Start -------------------------------------------------------------

    async def start(self, request: SearchRequest) -> int:
        session = self.session_factory()
        try:
            runtime = load_runtime_settings(session, self.settings)
            run = SearchRun(
                status=RUN_PENDING,
                request=request.model_dump(),
                progress={},
                errors=[],
                limitations=[],
            )
            session.add(run)
            session.commit()
            run_id = run.id
        finally:
            session.close()

        control = RunControl(run_id=run_id)
        self._controls[run_id] = control

        runner = SearchRunner(
            settings=self.settings,
            registry=self.registry,
            knowledge=self.knowledge,
        )
        task = asyncio.create_task(
            self._run(runner, run_id, request, control, runtime),
            name=f"search-run-{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished, rid=run_id: self._finalize(rid, finished))
        logger.info("Suchlauf %s gestartet", run_id)
        return run_id

    async def _run(
        self,
        runner: SearchRunner,
        run_id: int,
        request: SearchRequest,
        control: RunControl,
        runtime,
    ) -> None:
        session = self.session_factory()
        try:
            await runner.execute(run_id, request, control, runtime, session)
        finally:
            session.close()

    def _finalize(self, run_id: int, task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("Suchlauf %s wurde abgebrochen", run_id)
        elif task.exception() is not None:
            logger.error("Suchlauf %s endete mit Fehler: %s", run_id, task.exception())
            self._mark_failed(run_id, str(task.exception()))
        self._tasks.pop(run_id, None)

    def _mark_failed(self, run_id: int, message: str) -> None:
        session = self.session_factory()
        try:
            run = session.get(SearchRun, run_id)
            if run is not None and run.status in (RUN_PENDING, RUN_RUNNING):
                run.status = RUN_FAILED
                errors = list(run.errors or [])
                errors.append(message)
                run.errors = errors
                session.commit()
        finally:
            session.close()

    # -- Steuerung ---------------------------------------------------------

    def control(self, run_id: int) -> RunControl | None:
        return self._controls.get(run_id)

    def is_active(self, run_id: int) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    def active_run_ids(self) -> list[int]:
        return [rid for rid, task in self._tasks.items() if not task.done()]

    def pause(self, run_id: int) -> bool:
        control = self._controls.get(run_id)
        if control is None or not self.is_active(run_id):
            return False
        control.resume.clear()
        control.progress["paused"] = True
        control.progress["message"] = "Suchlauf pausiert. Er läuft weiter, sobald fortgesetzt wird."
        return True

    def resume(self, run_id: int) -> bool:
        control = self._controls.get(run_id)
        if control is None or not self.is_active(run_id):
            return False
        control.resume.set()
        control.progress["paused"] = False
        control.progress["message"] = "Suchlauf wird fortgesetzt."
        return True

    def cancel(self, run_id: int) -> bool:
        control = self._controls.get(run_id)
        if control is None or not self.is_active(run_id):
            # Ein bereits beendeter Lauf laesst sich nicht abbrechen: Sonst meldete
            # die API einen Abbruch, der nie stattfindet, und der gespeicherte
            # Zustand bliebe "beendet".
            return False
        control.cancel.set()
        control.resume.set()  # blockierte Worker aufwecken
        control.progress["cancelled"] = True
        control.progress["message"] = "Abbruch angefordert."
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        return True

    # -- Aufraeumen --------------------------------------------------------

    async def shutdown(self) -> None:
        for run_id in list(self._tasks):
            self.cancel(run_id)
        for task in list(self._tasks.values()):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        self._tasks.clear()

    def prune_controls(self, keep: int = 20) -> None:
        """Entfernt Steuerungsobjekte abgeschlossener Suchläufe."""
        if len(self._controls) <= keep:
            return
        finished = [
            run_id for run_id in self._controls if run_id not in self._tasks
        ]
        for run_id in finished[: max(0, len(self._controls) - keep)]:
            self._controls.pop(run_id, None)

    def latest_finished_run(self) -> int | None:
        session = self.session_factory()
        try:
            run = session.scalars(
                select(SearchRun)
                .where(SearchRun.status == RUN_COMPLETED)
                .order_by(SearchRun.id.desc())
                .limit(1)
            ).first()
            return run.id if run else None
        finally:
            session.close()
