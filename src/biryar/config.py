"""Settings, read from the environment.

One deployment mode, two shapes. A hosted install serves many tenants and
holds its own encryption key in a secret manager. A self-hosted install is
the same program with one tenant and the key in a file the operator controls.
Nothing in the code branches on which it is, because a second storage path is
a second set of bugs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .crypto import Keyring


class Deployment(StrEnum):
    """How this instance is run. Affects defaults and nothing structural."""

    HOSTED = "hosted"
    SELF_HOSTED = "self_hosted"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIRYAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deployment: Deployment = Deployment.SELF_HOSTED

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://biryar:biryar@localhost:5432/biryar"),
        description="SQLAlchemy URL. Must be PostgreSQL: tenant isolation is "
        "enforced with row-level security, which SQLite cannot express.",
    )

    #: Seals every tenant's Amazon credentials at rest, as one or more
    #: ``<id>:<fernet key>`` pairs, newest first. Losing every key means every
    #: tenant must reauthorise; leaking one means every credential it sealed
    #: is compromised.
    #:
    #: Deliberately not defaulted. A development default would be a key that
    #: ships in the repository and therefore protects nothing, and the day it
    #: reached production nothing would complain.
    secret_key: SecretStr = Field(default=SecretStr(""))

    #: Postgres role the application connects as. It must NOT be the table
    #: owner and must NOT have BYPASSRLS, or row-level security is advisory.
    #: See ``migrations`` and ``docs/architecture/tenancy.md``.
    db_role: str = "biryar_app"

    echo_sql: bool = False
    pool_size: int = 5
    max_overflow: int = 10

    @field_validator("database_url")
    @classmethod
    def _must_be_postgres(cls, value: PostgresDsn) -> PostgresDsn:
        if not str(value).startswith("postgresql"):
            raise ValueError(
                "Biryar requires PostgreSQL. Tenant isolation is enforced by "
                "row-level security, so there is no SQLite fallback."
            )
        return value

    @property
    def is_hosted(self) -> bool:
        return self.deployment is Deployment.HOSTED

    def keyring(self) -> Keyring:
        """The sealing keys. Raises if none are configured."""
        return Keyring.from_setting(self.secret_key.get_secret_value())


@lru_cache(maxsize=1)
def settings() -> Settings:
    """The process-wide settings, read once."""
    return Settings()
