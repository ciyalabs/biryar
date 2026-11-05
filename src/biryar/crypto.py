"""Sealing a tenant's Amazon refresh token.

A refresh token is a long-lived bearer credential for somebody's selling
account. It is worth more than the database row it sits in, and more than
most of the rows around it, so it is stored encrypted and the key lives
outside the database entirely — a secret manager on a hosted install, a file
the operator controls on a self-hosted one. A database dump is then not a
pile of working credentials.

## Why a keyring rather than a key

Keys get rotated: on a schedule, or in a hurry because one leaked. With a
single key, rotating means re-encrypting every row in one transaction, and
until that finishes the application cannot read what it just wrote. With a
keyring, new writes use the current key, old rows keep working, and
re-sealing happens in the background. The ``key_id`` column on
``amazon_credentials`` is what makes a row say which key it needs.

A rotation that cannot be done incrementally is a rotation that does not get
done, which is the real reason this is not one key.

## Why Fernet

Authenticated encryption with a sane construction and no parameters to get
wrong. The alternative is assembling AES-GCM by hand, which means owning
nonce generation — and a repeated nonce in GCM does not degrade security, it
destroys it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

#: ``id:key`` pairs. An id is short and opaque; it names a key and nothing
#: else, so it is safe in a database column and in a log line.
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SealingError(RuntimeError):
    """The keyring could not do what was asked of it."""


@dataclass(frozen=True)
class Sealed:
    """Ciphertext, and the id of the key that made it."""

    ciphertext: bytes
    key_id: str


class Keyring:
    """The keys this instance can decrypt with, and the one it encrypts with.

    Built from configuration by :meth:`from_setting`. Holding it as an object
    rather than a module global means a test can make one with a throwaway
    key, and a deployment can replace it without a restart.
    """

    def __init__(self, keys: dict[str, str], current: str) -> None:
        if not keys:
            raise SealingError("a keyring needs at least one key")
        if current not in keys:
            raise SealingError(f"the current key {current!r} is not in the keyring")

        self._fernets: dict[str, Fernet] = {}
        for key_id, material in keys.items():
            if not _KEY_ID.match(key_id):
                raise SealingError(f"{key_id!r} is not a usable key id")
            try:
                self._fernets[key_id] = Fernet(material.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise SealingError(f"key {key_id!r} is not a valid Fernet key") from exc

        self._current = current

    @classmethod
    def from_setting(cls, value: str) -> Keyring:
        """Parse ``v2:<key>,v1:<key>``. The first entry is the current key.

        Order carries meaning rather than a separate "which one is current"
        setting, because two settings that must agree are two settings that
        eventually will not.
        """
        entries = [part.strip() for part in value.split(",") if part.strip()]
        if not entries:
            raise SealingError(
                "no sealing key configured; set BIRYAR_SECRET_KEY to "
                "'<id>:<fernet key>'"
            )

        keys: dict[str, str] = {}
        order: list[str] = []
        for entry in entries:
            key_id, separator, material = entry.partition(":")
            if not separator:
                raise SealingError(
                    f"{entry[:16]!r} is missing its key id; write '<id>:<key>'"
                )
            if key_id in keys:
                raise SealingError(f"key id {key_id!r} appears twice")
            keys[key_id] = material
            order.append(key_id)

        return cls(keys, order[0])

    @staticmethod
    def generate_key() -> str:
        """A fresh Fernet key, for an operator setting an instance up."""
        return Fernet.generate_key().decode("ascii")

    @property
    def current_key_id(self) -> str:
        return self._current

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._fernets)

    def seal(self, plaintext: str) -> Sealed:
        """Encrypt with the current key."""
        if not plaintext:
            raise SealingError("refusing to seal an empty value")
        token = self._fernets[self._current].encrypt(plaintext.encode("utf-8"))
        return Sealed(ciphertext=token, key_id=self._current)

    def unseal(self, sealed: Sealed) -> str:
        """Decrypt, using whichever key made it.

        Raises rather than returning ``None`` when the key is gone. A caller
        cannot do anything sensible with "this credential exists but cannot
        be read", and a quiet empty string would be treated as a missing
        token and send the tenant through reauthorisation for no reason.
        """
        fernet = self._fernets.get(sealed.key_id)
        if fernet is None:
            raise SealingError(
                f"key {sealed.key_id!r} is not in the keyring, so this value "
                "cannot be read; it was sealed with a key this instance no "
                "longer has"
            )
        try:
            return fernet.decrypt(sealed.ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SealingError(
                f"the value sealed under {sealed.key_id!r} failed "
                "authentication; it was altered or the key is wrong"
            ) from exc

    def needs_resealing(self, sealed: Sealed) -> bool:
        """Whether this value is under an older key than the current one."""
        return sealed.key_id != self._current
