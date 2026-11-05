"""Version 1 of the HTTP API."""

from fastapi import APIRouter

from . import auth, health

router = APIRouter(prefix="/v1")
router.include_router(health.router)
router.include_router(auth.router)

__all__ = ["router"]
