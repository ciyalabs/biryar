"""Version 1 of the HTTP API."""

from fastapi import APIRouter

from . import amazon, auth, costs, health, profit

router = APIRouter(prefix="/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(amazon.router)
router.include_router(costs.router)
router.include_router(profit.router)

__all__ = ["router"]
