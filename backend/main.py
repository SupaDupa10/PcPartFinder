"""FastAPI-Anwendung: lokaler Start des Backends und der Oberfläche."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .config import Settings, get_settings
from .deps import AppState, build_state

logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

# Die Oberflaeche darf nie aus dem Browsercache kommen. Sonst zeigt ein Reload
# nach einem Neubau weiter die alte Fassung: Der Browser liefert die alte
# index.html (und damit das alte Bundle) ohne Rueckfrage aus seinem Cache.
NO_STORE_HEADERS = {"Cache-Control": "no-store, must-revalidate"}
IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


class ImmutableStaticFiles(StaticFiles):
    """Dateien unter /assets tragen einen Inhalts-Hash im Namen.

    Sie duerfen deshalb dauerhaft gecacht werden - ein neuer Build erzeugt
    einen neuen Dateinamen, es kann also nichts Veraltetes haengen bleiben.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    # Ein bereits gesetzter Zustand wird beibehalten. Das erlaubt es, die
    # Anwendung mit vorkonfigurierten Komponenten zu starten (Tests,
    # Einbettung) statt sie beim Start stillschweigend zu ersetzen.
    state: AppState | None = getattr(app.state, "container", None)
    if state is None:
        state = build_state(settings)
    app.state.container = state
    logger.info("Datenbank: %s", settings.database_url)
    logger.info("eBay konfiguriert: %s", settings.ebay_configured)

    try:
        yield
    finally:
        await state.manager.shutdown()
        await state.registry.aclose()
        state.engine.dispose()
        logger.info("Backend beendet.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Local AI Hardware Deal Hunter",
        description=(
            "Lokale Anwendung zur Suche und Bewertung gebrauchter PC-Hardware. "
            "Es werden ausschließlich zulässige Schnittstellen verwendet."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):  # pragma: no cover
        logger.exception("Unbehandelter Fehler bei %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    f"Unerwarteter Fehler: {type(exc).__name__}: {exc}. "
                    "Details stehen im Backend-Log."
                )
            },
        )

    @app.get("/api")
    def api_index() -> dict:
        return {
            "name": "Local AI Hardware Deal Hunter",
            "docs": "/docs",
            "endpoints": [
                "/api/system/status",
                "/api/system/categories",
                "/api/connectors",
                "/api/settings",
                "/api/knowledge",
                "/api/price-limits",
                "/api/search/runs",
                "/api/listings",
                "/api/stats/overview",
            ],
        }

    if FRONTEND_DIST.is_dir():
        app.mount(
            "/assets",
            ImmutableStaticFiles(directory=str(FRONTEND_DIST / "assets")),
            name="assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_frontend(full_path: str) -> FileResponse:
            candidate = FRONTEND_DIST / full_path
            if full_path and candidate.is_file():
                # Nur die gehashten Assets liegen hier; sie sind unveraenderlich.
                return FileResponse(candidate, headers=IMMUTABLE_HEADERS)
            return FileResponse(FRONTEND_DIST / "index.html", headers=NO_STORE_HEADERS)

    return app


app = create_app()


def run() -> None:  # pragma: no cover - Startskript
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
