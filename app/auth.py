"""Authentication and authorization dependencies."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

from .settings import settings


@dataclass(frozen=True)
class Principal:
    subject: str
    is_admin: bool


def _extract_token(authorization: str | None, x_aion_key: str | None) -> str:
    if x_aion_key:
        return x_aion_key.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            return value.strip()
    return ""


def _matches(token: str, candidates: tuple[str, ...]) -> bool:
    return any(hmac.compare_digest(token, candidate) for candidate in candidates)


def _subject(token: str) -> str:
    return "key_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_aion_key: str | None = Header(default=None, alias="X-AION-Key"),
) -> Principal:
    if not settings.auth_required:
        principal = Principal(subject="development", is_admin=True)
        request.state.principal = principal
        return principal
    token = _extract_token(authorization, x_aion_key)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication_required")
    is_admin = _matches(token, settings.admin_keys)
    if not is_admin and not _matches(token, settings.api_keys):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    principal = Principal(subject=_subject(token), is_admin=is_admin)
    request.state.principal = principal
    return principal


async def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return principal
