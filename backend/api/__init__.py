"""HTTP-API-Router."""

from fastapi import APIRouter

from .listings import router as listings_router
from .search import router as search_router
from .system import router as system_router

api_router = APIRouter(prefix="/api")
api_router.include_router(search_router)
api_router.include_router(listings_router)
api_router.include_router(system_router)

__all__ = ["api_router"]
