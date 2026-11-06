"""First run, against a database with nothing in it.

This is the gap that made a clean install unusable: migrations seed
nothing, there is no sign-up route, and sign-in needs a user that nothing
could create. So what is checked here is not that the command runs — it is
that someone who has only ever run `alembic upgrade head` can afterwards
sign in over HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.postgres


@pytest.fixture
def no_tenants(admin_engine: Engine) -> Iterator[None]:
    """A database with an empty tenants table, restored afterwards.

    The suite's other fixtures create tenants; this one is about their
    absence, which is the state a stranger starts from.
    """
    with admin_engine.begin() as conn:
        rows = list(
            conn.execute(
                text("SELECT id, slug, display_name, status::text FROM tenants")
            ).mappings()
        )
        conn.execute(text("DELETE FROM tenants"))
    yield
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants"))
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, display_name, status) "
                    "VALUES (:id, :slug, :display_name, CAST(:status AS tenant_status))"
                ),
                dict(row),
            )


def _run(monkeypatch: pytest.MonkeyPatch, engine: Engine, *argv: str) -> int:
    """Run the command against the test database."""
    import biryar.config
    import biryar.init
    from biryar.crypto import Keyring

    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("BIRYAR_DATABASE_URL", url)
    monkeypatch.setenv("BIRYAR_SECRET_KEY", f"v1:{Keyring.generate_key()}")
    biryar.config.settings.cache_clear()
    try:
        return biryar.init.main(list(argv))
    finally:
        biryar.config.settings.cache_clear()


def test_a_fresh_install_can_be_signed_into(
    client: TestClient, admin_engine: Engine, no_tenants: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point: empty database in, working sign-in out."""
    code = _run(
        monkeypatch, admin_engine,
        "--email", "owner@example.com",
        "--business", "Test Shop",
        "--password", "correct horse battery",
    )
    assert code == 0

    printed = capsys.readouterr().out
    assert "test-shop" in printed          # the slug it derived
    assert "owner@example.com" in printed
    # Supplied rather than generated, so it must not be echoed.
    assert "correct horse battery" not in printed

    signed_in = client.post(
        "/v1/auth/sign-in",
        json={"email": "owner@example.com", "password": "correct horse battery"},
        headers={"X-Biryar-Tenant": "test-shop"},
    )
    assert signed_in.status_code == 200, signed_in.text

    me = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {signed_in.json()['token']}"},
    )
    assert me.status_code == 200
    assert me.json()["role"] == "owner"


def test_a_generated_password_is_shown_once(
    admin_engine: Engine, no_tenants: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Nobody should have to put a password in their shell history."""
    assert _run(monkeypatch, admin_engine, "--email", "owner@example.com") == 0

    printed = capsys.readouterr().out
    assert "password:" in printed
    assert "only time it is shown" in printed


def test_it_refuses_to_quietly_make_a_second_tenant(
    admin_engine: Engine, no_tenants: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-running after a success is a mistake, not a request."""
    assert _run(monkeypatch, admin_engine, "--email", "first@example.com") == 0
    capsys.readouterr()

    assert _run(monkeypatch, admin_engine, "--email", "second@example.com") == 1
    assert "--allow-additional" in capsys.readouterr().err

    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 1


def test_a_bad_slug_is_explained_rather_than_raised(
    admin_engine: Engine, no_tenants: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A unique-constraint traceback is not an answer to a setup command."""
    code = _run(
        monkeypatch, admin_engine,
        "--email", "owner@example.com", "--slug", "Not A Slug",
    )
    assert code == 1
    assert "not a usable slug" in capsys.readouterr().err


def test_the_slug_rule_here_is_the_rule_the_database_enforces(
    admin_engine: Engine,
) -> None:
    """Validation that disagrees with the constraint is worse than none.

    Its whole purpose is to answer before Postgres does, with a sentence
    instead of a CheckViolation. If it is laxer the traceback comes back;
    if it is stricter it rejects slugs that would have been fine. Both have
    happened: the first version of this accepted a one-character slug the
    database refuses, and rejected "a--b", which it allows.

    So the two verdicts are compared directly rather than trusted.
    """
    from biryar.repositories.tenancy import SLUG

    candidates = [
        "ok", "abc", "a-b", "a--b", "shop", "a" * 63, "a" * 64,
        "a", "-ab", "ab-", "AB", "a_b", "a b", "", "sh-op-1", "1a",
    ]

    disagreements = []
    with admin_engine.connect() as conn:
        for candidate in candidates:
            in_postgres = bool(
                conn.execute(
                    text("SELECT :s ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'"),
                    {"s": candidate},
                ).scalar_one()
            )
            in_python = bool(SLUG.match(candidate))
            if in_postgres != in_python:
                disagreements.append(
                    f"{candidate!r}: postgres={in_postgres} python={in_python}"
                )

    assert not disagreements, (
        "the slug rule in Python and the one in the database disagree:\n  "
        + "\n  ".join(disagreements)
    )
