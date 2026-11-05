"""Signing in and out."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from ...db.models.tenancy import Role, User
from ...db.session import tenant_session
from ...repositories.auth import authenticate, issue_session, revoke_session
from ..deps import CurrentSession, Factory, TenantDb, TenantId

router = APIRouter(prefix="/auth", tags=["auth"])


class SignInRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class SignInResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserResponse


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Role

    @classmethod
    def of(cls, user: User) -> UserResponse:
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role,
        )


@router.post("/sign-in", response_model=SignInResponse)
def sign_in(
    body: SignInRequest,
    tenant_id: TenantId,
    factory: Factory,
    request: Request,
) -> SignInResponse:
    """Exchange credentials for a session token.

    One failure response for every cause — unknown address, wrong password,
    deactivated account. Distinguishing them turns this endpoint into a way
    to ask which email addresses are registered.

    The token is returned here and never again. Only its digest is stored.
    """
    with tenant_session(factory, tenant_id) as db:
        user = authenticate(db, tenant_id, body.email, body.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Those credentials do not match an account.",
            )
        issued = issue_session(
            db,
            user,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        return SignInResponse(
            token=issued.token,
            expires_at=issued.expires_at,
            user=UserResponse.of(user),
        )


@router.post("/sign-out", status_code=status.HTTP_204_NO_CONTENT)
def sign_out(session: CurrentSession, db: TenantDb) -> None:
    """End this session. Signing out twice is not an error."""
    revoke_session(db, session.session_id)


@router.get("/me", response_model=UserResponse)
def me(session: CurrentSession, db: TenantDb) -> UserResponse:
    """Who the presented token belongs to."""
    user = db.execute(
        select(User).where(User.id == session.user_id)
    ).scalar_one_or_none()
    if user is None:          # pragma: no cover - the session FK makes this unreachable
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    return UserResponse.of(user)


SignInResponse.model_rebuild()
