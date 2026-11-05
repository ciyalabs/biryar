"""The one sanctioned way to read a session before a tenant is known.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05

## The bootstrap problem

Every tenant-scoped table is readable only inside a transaction that has
already declared which tenant it is acting for. That is the whole design,
and it works right up to the request that has not authenticated yet.

A request arrives carrying a bearer token. To find out which tenant it
belongs to, the server has to read ``user_sessions``. But ``user_sessions``
is tenant-scoped, so reading it requires knowing the tenant — which is the
thing being looked up. Default-deny means the query returns nothing, and
nobody can ever sign in.

## Why not the obvious ways out

**Take sessions out of the tenant scope.** A session row carries a user, an
IP and a user agent. That is tenant data, and a table holding it should not
be the one table any tenant can read.

**Let the application bypass row-level security for this query.** The
application role would then need ``BYPASSRLS``, which switches the policy
off for *every* query it ever makes, to buy one exception. The test asserting
that role holds no such grant exists precisely to stop this.

**Keep a lookup table outside the scope.** A second place where tokens live,
to be kept in step with the first. Two sources of truth about who is signed
in is worse than the problem.

## What this does instead

One ``SECURITY DEFINER`` function. It runs as its owner, which is the role
that owns the tables and is therefore not subject to their policies, so it
can read ``user_sessions`` without a tenant in scope.

It is deliberately the narrowest hole that solves the problem:

* It takes an exact token digest. There is no prefix, pattern or range, so
  it cannot enumerate sessions — a caller must already hold the token.
* It returns four columns and no more. Not the IP, not the user agent, not
  the token digest back again. Only what deciding "is this session valid,
  and whose is it" requires.
* ``search_path`` is pinned. A ``SECURITY DEFINER`` function that resolves
  its own table names through the caller's ``search_path`` can be made to
  run against a table the caller created, with the owner's privileges. That
  is the classic way these become a privilege escalation, and an empty
  ``search_path`` with schema-qualified names closes it.
* It is ``STABLE`` and reads one row.

Every other read of tenant data still goes through a tenant transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: No new tables, so no new policies.
TENANT_TABLES: tuple[str, ...] = ()

RESOLVER = "biryar_resolve_session"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{RESOLVER}(p_token_hash text)
        RETURNS TABLE (
            session_id  uuid,
            tenant_id   uuid,
            user_id     uuid,
            expires_at  timestamptz,
            revoked_at  timestamptz
        )
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = ''
        AS $$
            SELECT s.id, s.tenant_id, s.user_id, s.expires_at, s.revoked_at
            FROM public.user_sessions AS s
            WHERE s.token_hash = p_token_hash
        $$
        """
    )

    # EXECUTE is granted explicitly rather than relying on the default. A
    # SECURITY DEFINER function is PUBLIC-executable unless told otherwise,
    # and "unless told otherwise" is not a thing to leave to chance on the
    # one function that can read any tenant's sessions.
    op.execute(f"REVOKE ALL ON FUNCTION public.{RESOLVER}(text) FROM PUBLIC")

    # The GRANT belongs to the deployment, not here. The application role is
    # created per deployment — a managed PostgreSQL often forbids CREATE ROLE
    # from a migration — and the same step that grants it table privileges
    # grants it EXECUTE on this. See docs/architecture/tenancy.md; the test
    # fixture does exactly what a deployment must.


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS public.{RESOLVER}(text)")
