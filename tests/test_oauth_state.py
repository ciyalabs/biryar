"""The state carried across the seller's authorisation. No database."""

from __future__ import annotations

import uuid

import pytest

from biryar.amazon.oauth import (
    STATE_TTL_SECONDS,
    OAuthStateError,
    begin,
    consent_url,
    sign_state,
    verify_state,
)
from biryar.db.models.credentials import Marketplace

SECRET = "a-test-secret"


def test_a_state_round_trips_to_its_tenant() -> None:
    tenant = uuid.uuid4()
    assert verify_state(sign_state(tenant, SECRET), SECRET) == tenant


def test_two_states_are_never_the_same() -> None:
    """A nonce, so that one state cannot be recognised as a reissue of another."""
    tenant = uuid.uuid4()
    assert sign_state(tenant, SECRET) != sign_state(tenant, SECRET)


def test_another_instance_cannot_mint_a_state_this_one_accepts() -> None:
    """The whole point: Amazon returns whatever was sent, so it must be ours."""
    state = sign_state(uuid.uuid4(), "someone-elses-secret")
    with pytest.raises(OAuthStateError, match="signature"):
        verify_state(state, SECRET)


def test_a_tampered_tenant_is_rejected() -> None:
    """Otherwise an authorisation could be steered into another account."""
    import base64

    tenant, attacker = uuid.uuid4(), uuid.uuid4()
    raw = base64.urlsafe_b64decode(
        (lambda s: s + "=" * (-len(s) % 4))(sign_state(tenant, SECRET))
    ).decode()
    forged = raw.replace(str(tenant), str(attacker))
    state = base64.urlsafe_b64encode(forged.encode()).decode().rstrip("=")

    with pytest.raises(OAuthStateError, match="signature"):
        verify_state(state, SECRET)


def test_an_expired_state_is_rejected() -> None:
    """Amazon's code lasts about five minutes; the state need not outlive it."""
    tenant = uuid.uuid4()
    state = sign_state(tenant, SECRET, now=1_000_000)
    verify_state(state, SECRET, now=1_000_000 + STATE_TTL_SECONDS - 1)
    with pytest.raises(OAuthStateError, match="expired"):
        verify_state(state, SECRET, now=1_000_000 + STATE_TTL_SECONDS + 1)


def test_a_state_from_the_future_is_rejected() -> None:
    state = sign_state(uuid.uuid4(), SECRET, now=1_000_000)
    with pytest.raises(OAuthStateError, match="future"):
        verify_state(state, SECRET, now=1_000_000 - 3600)


@pytest.mark.parametrize("junk", ["", "not-base64!!", "YWJj", "a:b:c:d"])
def test_junk_is_rejected_rather_than_crashing(junk: str) -> None:
    with pytest.raises(OAuthStateError):
        verify_state(junk, SECRET)


def test_the_signing_key_is_not_the_sealing_key() -> None:
    """Two purposes, two keys, so a signature oracle cannot decrypt anything."""
    from biryar.amazon.oauth import _signing_key

    assert _signing_key(SECRET) != SECRET.encode()
    assert len(_signing_key(SECRET)) == 32


# -- the consent URL -----------------------------------------------------------------

def test_the_consent_url_goes_to_the_right_seller_central() -> None:
    url = consent_url(Marketplace.DE, "amzn1.app", "the-state")
    assert url.startswith("https://sellercentral.amazon.de/apps/authorize/consent?")
    assert "application_id=amzn1.app" in url
    assert "state=the-state" in url
    assert "version=beta" not in url


def test_a_draft_application_asks_for_the_beta_version() -> None:
    """Required while the app is in Draft, rejected once published."""
    assert "version=beta" in consent_url(Marketplace.DE, "amzn1.app", "s", draft=True)


@pytest.mark.parametrize("marketplace", list(Marketplace))
def test_every_supported_marketplace_has_a_host(marketplace: Marketplace) -> None:
    """Claiming a marketplace with nowhere to send the seller is not support."""
    assert consent_url(marketplace, "amzn1.app", "s").startswith("https://sellercentral.")


def test_begin_mints_a_state_the_url_carries() -> None:
    tenant = uuid.uuid4()
    request = begin(tenant, Marketplace.DE, "amzn1.app", SECRET)
    assert request.state in request.url
    assert verify_state(request.state, SECRET) == tenant
