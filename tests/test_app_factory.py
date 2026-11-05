"""What create_app is actually built with. No database.

These exist because a factory that accepts configuration and then ignores it
is worse than one that takes none: every caller believes it works, and the
only symptom is behaviour from some other configuration entirely.

That happened. The settings dependency returned the process-wide cached
settings rather than the app's, so an app built with a test Amazon
application served requests as though none were configured, and nothing said
so until a route five layers up returned the wrong status.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from biryar.api import create_app
from biryar.api.deps import get_settings

from .conftest import settings_for_tests


def test_the_settings_a_route_sees_are_the_ones_passed_in() -> None:
    config = settings_for_tests(amazon_application_id="amzn1.sellerapps.app.distinctive")
    app = create_app(config)

    with TestClient(app):
        seen = app.state.settings

    assert seen is config
    assert seen.amazon_application_id == "amzn1.sellerapps.app.distinctive"


def test_two_apps_do_not_share_configuration() -> None:
    """Module-level caching would quietly make the second one win."""
    first = create_app(settings_for_tests(amazon_application_id="first"))
    second = create_app(settings_for_tests(amazon_application_id="second"))

    with TestClient(first), TestClient(second):
        assert first.state.settings.amazon_application_id == "first"
        assert second.state.settings.amazon_application_id == "second"


def test_the_settings_dependency_reads_the_app_not_the_process() -> None:
    """The dependency itself, rather than its effect somewhere downstream."""

    class _Request:
        def __init__(self, app: object) -> None:
            self.app = app

    config = settings_for_tests(amazon_application_id="from-the-app")
    app = create_app(config)

    with TestClient(app):
        assert get_settings(_Request(app)).amazon_application_id == "from-the-app"  # type: ignore[arg-type]


def test_an_installation_without_an_amazon_application_knows_it() -> None:
    """Checked before a seller is sent anywhere, not discovered mid-flow."""
    assert settings_for_tests().amazon_is_configured is True
    assert settings_for_tests(amazon_application_id="").amazon_is_configured is False
    assert settings_for_tests(lwa_client_secret="").amazon_is_configured is False
    assert settings_for_tests(amazon_redirect_uri="").amazon_is_configured is False
