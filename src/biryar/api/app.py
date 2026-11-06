"""The application factory.

## Why /v1 before the first route

The URL shape becomes a contract the moment anything consumes it, and the
React front end will. Adding the version afterwards means either breaking
every caller or serving both shapes forever. Adding it first costs five
characters.

## Why a factory

Tests build an app against a throwaway database and a clock they control.
A module-level ``app = FastAPI()`` reads its configuration at import, which
makes that impossible and makes the import order load-bearing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from .. import __version__
from ..config import Settings, settings
from ..db.session import engine_for, session_factory
from .static import mount as mount_static
from .v1 import router as v1_router


def create_app(
    config: Settings | None = None,
    *,
    engine: Engine | None = None,
) -> FastAPI:
    """Build the application.

    ``engine`` is injectable so a test can hand over one pointed at a
    throwaway database without the app reading the environment.
    """
    config = config or settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own = engine is None
        bound = engine or engine_for(config)
        app.state.settings = config
        app.state.engine = bound
        app.state.session_factory = session_factory(bound)
        try:
            yield
        finally:
            # Dispose only what this app opened. An injected engine belongs
            # to the caller, and closing it would break the next test that
            # shares it.
            if own:
                bound.dispose()

    app = FastAPI(
        title="Biryar",
        version=__version__,
        summary="Amazon operations, one decision at a time.",
        lifespan=lifespan,
        # The docs live under the version too, so a future /v2 can have its
        # own without either moving.
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
        redoc_url=None,
    )
    app.include_router(v1_router)

    # Mounted last. The catch-all route it adds would otherwise shadow the
    # API, and a route table is matched in the order it was built.
    mount_static(app, config.static_directory)

    return app
