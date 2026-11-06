"""First run: create the tenant and the owner who can sign in.

    python -m biryar.init --email you@example.com --business "Your Shop"

## Why this exists as a command

A fresh install had no way in at all. The migrations create tables and seed
nothing, there is no sign-up route, and ``POST /v1/auth/sign-in`` needs a
user that nothing could create — so ``docker compose up`` produced a
database, a healthy API, and a sign-in screen that could never be satisfied.
It worked for the author because the author's database already had rows.

## Why a command rather than a sign-up page

A sign-up page on a self-hosted install is a door onto the public internet
whose only job is to be used exactly once. Whoever reaches it first becomes
the owner. Making the first account from the shell means the person holding
it is the person who can already read the database, which is the same
authority, honestly expressed.

A hosted deployment needs real sign-up, with email verification and a
tenant per customer. That is a different thing and it is not this.
"""

from __future__ import annotations

import argparse
import secrets
import sys

from sqlalchemy.exc import ProgrammingError

from .config import settings
from .db.models.tenancy import Role
from .db.session import admin_session, engine_for, session_factory, tenant_session
from .repositories import (
    SlugError,
    create_tenant,
    create_user,
    normalise_email,
    slugify,
    tenant_count,
)

#: Long enough that a generated one is not worth attacking, short enough to
#: be retyped from a terminal into a browser without resentment.
GENERATED_PASSWORD_BYTES = 12


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="biryar.init",
        description="Create the first tenant and owner.",
    )
    parser.add_argument("--email", required=True, help="the owner's sign-in email")
    parser.add_argument(
        "--business",
        default=None,
        help="the business name, shown in the interface (default: from the email)",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="short url-safe name (default: derived from the business name)",
    )
    parser.add_argument("--name", default=None, help="the owner's display name")
    parser.add_argument(
        "--password",
        default=None,
        help="leave this out and one is generated and printed, which is safer "
             "than a password that was in your shell history",
    )
    parser.add_argument(
        "--allow-additional",
        action="store_true",
        help="create another tenant even though one already exists",
    )
    args = parser.parse_args(argv)

    config = settings()
    if not config.secret_key.get_secret_value():
        print(
            "BIRYAR_SECRET_KEY is not set. Generate one with:\n"
            "    python -c 'from biryar.crypto import Keyring; "
            "print(\"v1:\" + Keyring.generate_key())'",
            file=sys.stderr,
        )
        return 2

    email = normalise_email(args.email)
    business = args.business or email.split("@")[0]
    slug = args.slug or slugify(business)
    display_name = args.name or email.split("@")[0]
    password = args.password or secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    generated = args.password is None

    engine = engine_for(config)
    factory = session_factory(engine)

    try:
        with admin_session(factory) as admin:
            existing = tenant_count(admin)
            if existing and not args.allow_additional:
                print(
                    f"This installation already has {existing} tenant(s). "
                    "Re-running would create another, which on a self-hosted "
                    "install is almost never what is wanted — pass "
                    "--allow-additional if it is.",
                    file=sys.stderr,
                )
                return 1
            tenant = create_tenant(admin, slug=slug, display_name=business)
            tenant_id = tenant.id
    except SlugError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except ProgrammingError:
        # The usual cause by a wide margin, and the error Postgres gives for
        # it names a table rather than the step that was skipped.
        print(
            "The database does not have Biryar's tables. Run the migrations "
            "first:\n    alembic upgrade head",
            file=sys.stderr,
        )
        return 1

    with tenant_session(factory, tenant_id) as db:
        create_user(
            db,
            tenant_id,
            email=email,
            display_name=display_name,
            role=Role.OWNER,
            password=password,
        )

    print(f"Created {business!r} (slug: {slug})")
    print(f"  owner: {email}")
    if generated:
        print(f"  password: {password}")
        print("\nWrite the password down now — it is not stored anywhere in")
        print("readable form and this is the only time it is shown.")
    print("\nSign in at http://localhost:8000/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
