"""Serving the single-page application alongside the API.

The reload case is the one worth testing. A static mount makes the app work
when clicked into and 404 when reloaded, which survives testing indefinitely
because nobody reloads while developing.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from biryar.api import create_app

from .conftest import settings_for_tests


@pytest.fixture
def built(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory shaped like the front end's build output."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Biryar</title>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


@pytest.fixture
def client(built: pathlib.Path) -> Iterator[TestClient]:
    """The app with a front end, started properly.

    The lifespan has to run: it is what puts the session factory on the app,
    and an API route reads it before deciding anyone is unauthenticated. No
    database is contacted — the engine connects lazily, and these tests
    never get as far as a query.
    """
    app = create_app(settings_for_tests(static_dir=str(built)))
    with TestClient(app) as started:
        yield started


def test_the_root_serves_the_application(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Biryar" in response.text


def test_a_client_side_route_survives_a_reload(client: TestClient) -> None:
    """The bug this exists to prevent.

    /settings is a route the browser handles, not a file. A plain static
    mount answers 404, so the page works when clicked into and breaks when
    reloaded or opened from a bookmark.
    """
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Biryar" in response.text


def test_real_files_are_served_as_themselves(client: TestClient) -> None:
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/index-abc123.js").text == "console.log(1)"


def test_the_api_keeps_its_own_responses(client: TestClient) -> None:
    """A mistyped API path must not come back as a page full of markup.

    Answering HTML would turn a 404 into a 200 and move the failure from the
    call to whatever tried to parse the response.
    """
    response = client.get("/v1/auth/not-a-real-route")
    assert response.status_code == 404
    assert "<!doctype html>" not in response.text.lower()


def test_an_api_route_that_exists_still_works(client: TestClient) -> None:
    # 401 rather than HTML: the route was reached and refused.
    assert client.get("/v1/auth/me").status_code == 401


def test_the_api_runs_without_a_front_end(tmp_path: pathlib.Path) -> None:
    """A development API has its front end on a dev server, not on disk."""
    app = create_app(settings_for_tests(static_dir=str(tmp_path / "nothing")))
    with TestClient(app) as client:
        assert client.get("/v1/auth/me").status_code == 401
        assert client.get("/").status_code == 404


def test_a_path_cannot_escape_the_build_directory(
    client: TestClient, built: pathlib.Path,
) -> None:
    """Otherwise the static mount is a way to read the filesystem."""
    secret = built.parent / "secret.txt"
    secret.write_text("not for serving")

    response = client.get("/../secret.txt")

    assert "not for serving" not in response.text
