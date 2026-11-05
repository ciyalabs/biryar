"""Tenants, the people inside them, and their sessions.

A **tenant** is one selling business. A hosted install has many; a
self-hosted install has exactly one, created at first run. Nothing in the
code distinguishes the two cases, so the isolation that protects a hosted
customer is the same isolation a self-hoster never has to think about.

A **user** belongs to exactly one tenant. Someone who works for two selling
businesses gets two accounts, because the alternative — a user with a list of
tenants — turns every permission check into a join and makes the
row-level-security policy unable to decide anything on its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, ShortText, TenantScoped, Timestamp, TimestampMixin, tenant_fk


class Role(StrEnum):
    """What a user may do inside their tenant.

    Deliberately two. The platform has exactly one dangerous category of
    action — anything that reaches Amazon, a buyer, a supplier or money — and
    a role system with five tiers invites the question of which tier may do
    half of it. An owner may approve; an assistant may prepare.
    """

    OWNER = "owner"
    ASSISTANT = "assistant"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class Tenant(Base, TimestampMixin):
    """One selling business.

    Not tenant-scoped, for the obvious reason: this is the table the scope is
    defined against. It is readable only through an admin session.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    #: Short, stable, URL-safe. Used in hosted subdomains and in log lines,
    #: never as a foreign key, so it can be changed without a data migration.
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    display_name: Mapped[ShortText] = mapped_column(nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status", native_enum=True),
        nullable=False,
        default=TenantStatus.ACTIVE,
    )

    users: Mapped[list[User]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'",
            name="slug_is_url_safe",
        ),
    )


class User(Base, TenantScoped, TimestampMixin):
    """A person who signs in, inside exactly one tenant."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[ShortText] = mapped_column(nullable=False)

    #: Argon2id. Null means the account cannot sign in: either it was revoked,
    #: or it was provisioned and has not set a password yet. The row stays
    #: either way, because deleting a user anonymises every audit entry that
    #: points at them.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", native_enum=True), nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Unique per tenant, not globally: the same person may legitimately
        # hold an account in two selling businesses.
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        CheckConstraint("position('@' in email) > 1", name="email_has_a_local_part"),
    )


class UserSession(Base, TenantScoped):
    """A signed-in browser session.

    Only the hash of the token is stored. A database dump, a backup or a log
    line therefore cannot be replayed as a login, which is the whole point of
    not storing the token itself.
    """

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True,
    )

    #: SHA-256 of the bearer token. Not Argon2: this value is already a
    #: 256-bit random string, so it needs no stretching, and a login path
    #: that runs a slow KDF on every request is a denial-of-service lever.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    issued_at: Mapped[Timestamp] = mapped_column(
        server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[Timestamp] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_user_sessions_expires_at", "expires_at"),
        CheckConstraint("expires_at > issued_at", name="expires_after_issue"),
    )
