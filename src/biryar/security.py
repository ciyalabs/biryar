"""Password hashing and session tokens.

Pure. No database, no framework, no request object — just the handful of
decisions that are easy to get subtly wrong and expensive to get wrong
quietly.

## Two secrets, two treatments

A **password** is chosen by a human, so it is low-entropy and must be made
expensive to guess. It goes through Argon2id, which is deliberately slow and
memory-hard.

A **session token** is generated here from the system CSPRNG, so it already
carries 256 bits of entropy and there is nothing to strengthen. It is stored
as a plain SHA-256 digest. Running Argon2 over it instead would add no
security — an attacker cannot brute-force 256 random bits whatever the hash —
and would put a deliberately slow function on the path of *every*
authenticated request, which is a denial-of-service lever rather than a
defence.

The common mistake is applying the first treatment to the second, so the
reasoning is written down rather than assumed.

## What is stored is never what is presented

The database holds the digest, never the token. A dump, a backup or a log
line therefore cannot be replayed as a login. The token exists in plaintext
exactly once, in the response that issues it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Argon2id at the library's current defaults, which track the RFC 9106
#: recommendations. Pinned behind one object so that raising the cost later
#: is a one-line change, and so that every call site agrees.
_hasher = PasswordHasher()

#: 32 bytes from the system CSPRNG. ``token_urlsafe`` renders it as 43
#: characters, which fits an Authorization header and a cookie without
#: encoding games.
TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """Argon2id digest of ``password``, salt included in the output."""
    if not password:
        raise ValueError("refusing to hash an empty password")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Whether ``password`` matches ``password_hash``.

    ``None`` means the account cannot sign in — revoked, or provisioned and
    never given a password. It returns False rather than raising, because to
    a caller that is the same answer as a wrong password and should take the
    same path.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether this digest was made with parameters weaker than current.

    Checked at sign-in, when the plaintext is in hand and rehashing is free.
    Without it, raising the cost protects only accounts created afterwards,
    which is the opposite of what is wanted: the oldest passwords are the
    ones most likely to have been exposed elsewhere.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_session_token() -> str:
    """A fresh bearer token. Returned once and never recoverable."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """The digest stored for ``token``: SHA-256, hex, 64 characters."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(presented: str, stored_hash: str) -> bool:
    """Compare in constant time.

    ``==`` on strings returns as soon as two bytes differ, and the time it
    took is measurable over enough requests. The leak is small and entirely
    avoidable, so it is avoided.
    """
    return hmac.compare_digest(hash_session_token(presented), stored_hash)
