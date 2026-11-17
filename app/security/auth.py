"""Who is calling: turn an HTTP request into an employee ID.

The employee's role, region and manager always come from veltrix.db; this module
only decides which employee ID the request may act as. Three modes (AUTH_MODE):

- ``header``  — trust ``X-Employee-Id`` as sent. Development and the original
  assessment contract only; refused when APP_ENV=production.
- ``gateway`` — an authenticating reverse proxy (API gateway, OAuth2 proxy, SSO
  sidecar) sets ``X-Employee-Id`` and proves it did so with
  ``X-Gateway-Token: <AUTH_GATEWAY_TOKEN>``. Clients must not be able to reach
  the service except through that proxy.
- ``jwt``     — ``Authorization: Bearer <token>`` signed by your identity provider;
  the employee ID is read from AUTH_JWT_EMPLOYEE_CLAIM (default ``sub``).
  ``X-Employee-Id`` is optional; if present it must match the token.

Any failure is reported as the same 401 so callers cannot probe which part failed.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Mapping, Optional

from app.config import Settings

log = logging.getLogger(__name__)

UNAUTHENTICATED = "missing or invalid credentials"
MAX_EMPLOYEE_ID_CHARS = 64


@dataclass(frozen=True)
class AuthResult:
    employee_id: Optional[str]
    reason: str = ""  # for logs only, never returned to the caller


def _clean_id(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > MAX_EMPLOYEE_ID_CHARS or not value.isprintable():
        return None
    return value


def authenticate(headers: Mapping[str, str], settings: Settings) -> AuthResult:
    """Return the employee ID the request is allowed to act as, or None."""
    header_id = _clean_id(headers.get("x-employee-id"))
    mode = settings.auth_mode

    if mode == "header":
        return AuthResult(header_id, "" if header_id else "missing X-Employee-Id")

    if mode == "gateway":
        presented = headers.get("x-gateway-token") or ""
        if not settings.gateway_token or not hmac.compare_digest(presented.encode(),
                                                                 settings.gateway_token.encode()):
            return AuthResult(None, "bad gateway token")
        return AuthResult(header_id, "" if header_id else "missing X-Employee-Id")

    if mode == "jwt":
        auth = headers.get("authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return AuthResult(None, "missing bearer token")
        claim_id, reason = _verify_jwt(token.strip(), settings)
        if claim_id is None:
            return AuthResult(None, reason)
        if headers.get("x-employee-id") is not None and header_id != claim_id:
            return AuthResult(None, "X-Employee-Id does not match token")
        return AuthResult(claim_id)

    return AuthResult(None, f"unknown auth mode {mode}")


def _verify_jwt(token: str, settings: Settings) -> tuple[Optional[str], str]:
    import jwt  # PyJWT

    symmetric = all(a.upper().startswith("HS") for a in settings.jwt_algorithms)
    key = settings.jwt_secret if symmetric else settings.jwt_public_key
    options = {"require": ["exp", settings.jwt_employee_claim], "verify_aud": bool(settings.jwt_audience)}
    try:
        claims = jwt.decode(
            token, key, algorithms=list(settings.jwt_algorithms),
            audience=settings.jwt_audience or None, issuer=settings.jwt_issuer or None,
            options=options, leeway=30,
        )
    except jwt.ExpiredSignatureError:
        return None, "token expired"
    except jwt.PyJWTError as exc:
        return None, f"invalid token ({type(exc).__name__})"
    employee_id = _clean_id(claims.get(settings.jwt_employee_claim))
    return (employee_id, "") if employee_id else (None, "token has no usable employee claim")
