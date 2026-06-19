"""
Authentication & authorisation for the MCP Data Agents API.

Two modes — controlled by AUTH_MODE env var:

  api_key  (default, simplest)
    Client sends:  X-API-Key: <key>
    Server checks: key is in API_KEYS env var (comma-separated list)
    TenantContext: extracted from X-Tenant-ID / X-User-ID / X-Team-ID headers
                   (validated against ALLOWED_TENANTS allowlist)

  jwt      (production SSO path)
    Client sends:  Authorization: Bearer <JWT>
    Server checks: signature against JWKS_URL, expiry, required claims
    TenantContext: extracted from JWT claims (tid / sub / team)

Set AUTH_MODE=none to disable auth entirely (local dev / tests only).

Environment variables:
  AUTH_MODE        api_key | jwt | none        (default: api_key)
  API_KEYS         comma-separated valid keys   (api_key mode)
  ALLOWED_TENANTS  comma-separated tenant IDs   (empty = allow any)
  JWKS_URL         URL to IdP JWKS endpoint     (jwt mode)
  JWT_AUDIENCE     expected 'aud' claim          (jwt mode)
  JWT_ISSUER       expected 'iss' claim          (jwt mode)
"""
import os
import time
from functools import lru_cache
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from tenant import TenantContext

# ── Config ────────────────────────────────────────────────────────────────────

AUTH_MODE = os.environ.get("AUTH_MODE", "api_key").lower()

_raw_keys      = os.environ.get("API_KEYS", "")
_API_KEYS: set[str] = {k.strip() for k in _raw_keys.split(",") if k.strip()}

_raw_tenants      = os.environ.get("ALLOWED_TENANTS", "")
_ALLOWED_TENANTS: set[str] = {t.strip() for t in _raw_tenants.split(",") if t.strip()}

JWKS_URL     = os.environ.get("JWKS_URL", "")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "mcp-data-agents")
JWT_ISSUER   = os.environ.get("JWT_ISSUER", "")

# ── FastAPI security schemes ──────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme  = HTTPBearer(auto_error=False)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate_tenant(tenant_id: str) -> str:
    """Return tenant_id if allowed, raise 403 otherwise."""
    if _ALLOWED_TENANTS and tenant_id not in _ALLOWED_TENANTS:
        raise HTTPException(
            status_code=403,
            detail=f"Tenant '{tenant_id}' is not authorised.",
        )
    return tenant_id


def _tenant_from_headers(request: Request) -> TenantContext:
    h = request.headers
    return TenantContext(
        tenant_id=_validate_tenant(h.get("X-Tenant-ID", "default")),
        user_id=h.get("X-User-ID"),
        team_id=h.get("X-Team-ID"),
    )


def _tenant_from_jwt(token: str) -> TenantContext:
    """
    Decode and validate a JWT, then extract tenant context from claims.

    Requires:  pip install python-jose[cryptography] httpx
    JWKS is fetched once and cached.  The 'tid' claim is the tenant ID;
    'sub' is the user; 'team' is optional cost-centre.

    Swap this implementation for your IdP's SDK (Cognito, Auth0, Okta, etc.)
    without touching the rest of the auth module.
    """
    try:
        from jose import JWTError, jwt as jose_jwt
        import httpx

        jwks = _fetch_jwks()
        claims = jose_jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER or None,
        )
        return TenantContext(
            tenant_id=_validate_tenant(claims.get("tid", "default")),
            user_id=claims.get("sub"),
            team_id=claims.get("team"),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Fetch JWKS once and cache for the process lifetime."""
    import httpx
    if not JWKS_URL:
        raise HTTPException(status_code=500, detail="JWKS_URL not configured.")
    resp = httpx.get(JWKS_URL, timeout=5)
    resp.raise_for_status()
    return resp.json()


# ── Public FastAPI dependency ─────────────────────────────────────────────────

async def require_auth(
    request: Request,
    api_key: Optional[str]                         = Security(_api_key_header),
    bearer:  Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> TenantContext:
    """
    FastAPI dependency — attach to any route that needs auth.

    Usage:
        @app.post("/query")
        async def query(req: QueryRequest, ctx: TenantContext = Depends(require_auth)):
            ...

    Returns TenantContext on success, raises HTTP 401/403 on failure.
    """
    if AUTH_MODE == "none":
        return _tenant_from_headers(request)

    if AUTH_MODE == "api_key":
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing X-API-Key header.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if api_key not in _API_KEYS:
            from logging_config import get_logger
            get_logger(__name__).warning("auth.rejected", reason="invalid_api_key")
            raise HTTPException(status_code=401, detail="Invalid API key.")
        return _tenant_from_headers(request)

    if AUTH_MODE == "jwt":
        if not bearer:
            raise HTTPException(
                status_code=401,
                detail="Missing Authorization: Bearer <token> header.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _tenant_from_jwt(bearer.credentials)

    raise HTTPException(status_code=500, detail=f"Unknown AUTH_MODE: {AUTH_MODE}")
