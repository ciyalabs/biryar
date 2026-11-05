"""Sealing. No database, no network."""

from __future__ import annotations

import pytest

from biryar.crypto import Keyring, Sealed, SealingError


@pytest.fixture
def keyring() -> Keyring:
    return Keyring({"v1": Keyring.generate_key()}, "v1")


def test_a_sealed_value_comes_back(keyring: Keyring) -> None:
    sealed = keyring.seal("Atzr|a-refresh-token")
    assert keyring.unseal(sealed) == "Atzr|a-refresh-token"


def test_the_ciphertext_does_not_contain_the_plaintext(keyring: Keyring) -> None:
    """The point of the exercise."""
    sealed = keyring.seal("Atzr|a-refresh-token")
    assert b"refresh-token" not in sealed.ciphertext
    assert b"Atzr" not in sealed.ciphertext


def test_sealing_twice_gives_different_ciphertext(keyring: Keyring) -> None:
    """Otherwise equal tokens are visibly equal in the table."""
    assert keyring.seal("same").ciphertext != keyring.seal("same").ciphertext


def test_tampering_is_detected(keyring: Keyring) -> None:
    """Fernet authenticates; a flipped byte must fail, not decrypt to garbage."""
    sealed = keyring.seal("Atzr|a-refresh-token")
    altered = Sealed(ciphertext=sealed.ciphertext[:-1] + b"x", key_id=sealed.key_id)
    with pytest.raises(SealingError, match="altered"):
        keyring.unseal(altered)


def test_another_instances_key_cannot_read_it() -> None:
    mine = Keyring({"v1": Keyring.generate_key()}, "v1")
    theirs = Keyring({"v1": Keyring.generate_key()}, "v1")
    with pytest.raises(SealingError):
        theirs.unseal(mine.seal("secret"))


def test_refusing_to_seal_nothing(keyring: Keyring) -> None:
    with pytest.raises(SealingError, match="empty"):
        keyring.seal("")


# -- rotation ------------------------------------------------------------------------

def test_an_old_key_still_reads_what_it_sealed() -> None:
    """Rotation has to be incremental, or it does not happen at all."""
    old, new = Keyring.generate_key(), Keyring.generate_key()
    before = Keyring({"v1": old}, "v1")
    sealed = before.seal("Atzr|a-refresh-token")

    after = Keyring({"v2": new, "v1": old}, "v2")

    assert after.unseal(sealed) == "Atzr|a-refresh-token"
    assert after.needs_resealing(sealed)
    assert not after.needs_resealing(after.seal("x"))


def test_a_value_under_a_discarded_key_fails_loudly() -> None:
    """Not an empty string: that would look like a missing token.

    A tenant whose credential silently read as absent would be sent through
    reauthorisation for no reason, and the real cause — a key dropped from
    the keyring — would never be mentioned.
    """
    sealed = Keyring({"v1": Keyring.generate_key()}, "v1").seal("x")
    only_new = Keyring({"v2": Keyring.generate_key()}, "v2")
    with pytest.raises(SealingError, match="no longer has"):
        only_new.unseal(sealed)


# -- configuration -------------------------------------------------------------------

def test_the_first_configured_key_is_the_one_that_seals() -> None:
    a, b = Keyring.generate_key(), Keyring.generate_key()
    keyring = Keyring.from_setting(f"v2:{a},v1:{b}")
    assert keyring.current_key_id == "v2"
    assert keyring.key_ids == {"v1", "v2"}
    assert keyring.seal("x").key_id == "v2"


def test_a_key_without_an_id_is_refused() -> None:
    """An unlabelled key cannot be rotated away from later."""
    with pytest.raises(SealingError, match="missing its key id"):
        Keyring.from_setting(Keyring.generate_key())


def test_an_empty_setting_says_what_to_set() -> None:
    with pytest.raises(SealingError, match="BIRYAR_SECRET_KEY"):
        Keyring.from_setting("   ")


def test_a_repeated_key_id_is_refused() -> None:
    """Otherwise which key 'v1' means depends on dictionary ordering."""
    a, b = Keyring.generate_key(), Keyring.generate_key()
    with pytest.raises(SealingError, match="twice"):
        Keyring.from_setting(f"v1:{a},v1:{b}")


def test_a_key_that_is_not_a_key_is_refused_at_startup() -> None:
    """Better than discovering it on the first credential written."""
    with pytest.raises(SealingError, match="not a valid Fernet key"):
        Keyring.from_setting("v1:not-a-real-key")
