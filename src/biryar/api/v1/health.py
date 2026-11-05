"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from ... import __version__
from ..deps import Factory

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str
    database: str


@router.get("/health", response_model=Health)
def health(factory: Factory) -> Health:
    """Whether this instance can serve.

    The database is actually queried rather than assumed. A process that is
    up but cannot reach PostgreSQL serves nothing useful, and a health check
    that returns ok regardless is how that goes unnoticed through a deploy.
    """
    session = factory()
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unreachable"
    finally:
        session.close()

    return Health(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        database=database,
    )
