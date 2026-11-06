"""Creating the thing every other table is scoped to.

``tenants`` has no row-level-security policy, because it is the table the
policy is defined against: a tenant cannot be looked up by a session that
has to name a tenant to see anything. So everything here needs an admin
session, and there is deliberately very little of it.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.tenancy import Tenant, TenantStatus

#: The same rule the database enforces, character for character.
#:
#: It is repeated here rather than inferred because the point is to answer
#: before Postgres does: a CheckViolation traceback is not a reply to give
#: someone running a setup command. The two agreeing is not left to care —
#: ``test_init.py`` asserts that this pattern and the live constraint reach
#: the same verdict on the same candidates, so a change to one that forgets
#: the other fails rather than drifting.
#:
#: Three characters minimum, 63 maximum (a DNS label, since a hosted
#: install puts the slug in front of a domain), no leading or trailing
#: hyphen.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"
SLUG = re.compile(SLUG_PATTERN)

SLUG_MIN = 3
SLUG_MAX = 63


class SlugError(ValueError):
    """The slug is not usable as a subdomain or is already taken."""


def slugify(name: str) -> str:
    """A usable slug from a display name, for when nobody supplied one."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def tenant_count(session: Session) -> int:
    """How many tenants exist. Admin session."""
    return len(list(session.execute(select(Tenant.id)).scalars()))


def create_tenant(session: Session, *, slug: str, display_name: str) -> Tenant:
    """Create one tenant. Admin session.

    Validates the slug here rather than letting the unique index speak,
    because "duplicate key value violates unique constraint" is not an
    answer to give someone running a setup command for the first time.
    """
    if not SLUG.match(slug):
        raise SlugError(
            f"{slug!r} is not a usable slug. It needs {SLUG_MIN} to "
            f"{SLUG_MAX} characters, lower-case letters, digits and hyphens, "
            "and may not start or end with a hyphen. Pass --slug to choose "
            "one."
        )

    taken = session.execute(
        select(Tenant.id).where(Tenant.slug == slug)
    ).scalar_one_or_none()
    if taken is not None:
        raise SlugError(f"a tenant with the slug {slug!r} already exists")

    tenant = Tenant(slug=slug, display_name=display_name, status=TenantStatus.ACTIVE)
    session.add(tenant)
    session.flush()
    return tenant
