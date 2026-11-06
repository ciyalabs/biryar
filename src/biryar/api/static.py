"""Serving the built front end from the same process as the API.

A self-hosted install should be one thing to run, not two. The API already
has a process and a port; giving it the thirty lines needed to serve a
directory of files is cheaper for everyone than a second container, a second
port and a reverse proxy to put them back together.

## Why a catch-all rather than StaticFiles alone

The front end is a single-page application: ``/settings`` is a route it
handles itself, not a file on disk. A plain static mount answers 404 for it,
so the application works when clicked into but not when reloaded or
bookmarked — a bug that reliably survives testing, because nobody reloads.

So anything that is not an API path and not a real file gets index.html, and
the router in the browser decides what it means.

## What must not be swallowed

Everything under the API's own prefixes. Returning the HTML shell for a
mistyped API path would turn a 404 into a 200 full of markup, and the client
would fail while parsing rather than at the call.
"""

from __future__ import annotations

import logging
import pathlib

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

#: Paths that belong to the API and must keep their own responses.
API_PREFIXES = ("/v1",)


def mount(app: FastAPI, directory: pathlib.Path) -> bool:
    """Serve ``directory`` under the application. Whether anything was found.

    Missing is not an error. A development API runs with the front end on
    its own dev server, and a container that failed to build the assets
    should say so at build time rather than refusing to start.
    """
    index = directory / "index.html"
    if not index.is_file():
        logger.info(
            "static: no front end at %s; serving the API only", directory,
        )
        return False

    assets = directory / "assets"
    if assets.is_dir():
        # Hashed filenames, so they can be cached hard.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(request: Request, path: str) -> FileResponse:
        if any(f"/{path}".startswith(prefix) for prefix in API_PREFIXES):
            # Let the API's own 404 happen rather than answering with HTML.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        candidate = (directory / path).resolve()
        if (
            path
            and directory.resolve() in candidate.parents
            and candidate.is_file()
        ):
            # A real file: favicon, robots.txt, anything else shipped.
            return FileResponse(candidate)

        # Everything else is a route the browser handles.
        return FileResponse(index)

    logger.info("static: serving the front end from %s", directory)
    return True
