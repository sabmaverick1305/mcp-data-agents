"""
Tests for auth.py — API key authentication, passthrough mode, and tenant allowlisting.

Test structure:
  TestApiKeyMode         AUTH_MODE=api_key with two valid keys (secret-key-1, secret-key-2)
                         Tests: valid key returns TenantContext, missing key → 401,
                         invalid key → 401, missing X-Tenant-ID header → "default" tenant.

  TestNoneMode           AUTH_MODE=none (local dev passthrough)
                         Tests: any request reaches the endpoint without an API key.

  TestTenantAllowlist    AUTH_MODE=api_key with ALLOWED_TENANTS="acme,globex"
                         Tests: allowed tenant passes, unlisted tenant → 403.

Each test class uses monkeypatch + importlib.reload(auth) to re-initialise the module
with the patched environment variables. This is necessary because auth.py reads env vars
at import time, so reloading forces a fresh read.

All tests are async (pytest-asyncio) because require_auth is an async FastAPI dependency.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_request(headers: dict):
    req = MagicMock()
    req.headers = headers
    return req


def _make_api_key_security(key: str | None):
    return key


def _make_bearer(token: str | None):
    if token is None:
        return None
    cred = MagicMock()
    cred.credentials = token
    return cred


# ── api_key mode ──────────────────────────────────────────────────────────────

class TestApiKeyMode:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "api_key")
        monkeypatch.setenv("API_KEYS", "secret-key-1,secret-key-2")
        monkeypatch.setenv("ALLOWED_TENANTS", "")
        # Reload module so env vars are picked up
        import importlib
        import auth as auth_mod
        importlib.reload(auth_mod)
        self.auth = auth_mod

    @pytest.mark.asyncio
    async def test_valid_key_returns_tenant_context(self):
        req = _make_request({"X-Tenant-ID": "acme", "X-User-ID": "alice"})
        ctx = await self.auth.require_auth(req, api_key="secret-key-1", bearer=None)
        assert ctx.tenant_id == "acme"
        assert ctx.user_id == "alice"

    @pytest.mark.asyncio
    async def test_missing_key_raises_401(self):
        from fastapi import HTTPException
        req = _make_request({})
        with pytest.raises(HTTPException) as exc_info:
            await self.auth.require_auth(req, api_key=None, bearer=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_key_raises_401(self):
        from fastapi import HTTPException
        req = _make_request({"X-Tenant-ID": "acme"})
        with pytest.raises(HTTPException) as exc_info:
            await self.auth.require_auth(req, api_key="wrong-key", bearer=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_default_tenant_when_header_absent(self):
        req = _make_request({})
        ctx = await self.auth.require_auth(req, api_key="secret-key-2", bearer=None)
        assert ctx.tenant_id == "default"


# ── none mode (dev) ───────────────────────────────────────────────────────────

class TestNoneMode:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "none")
        monkeypatch.setenv("ALLOWED_TENANTS", "")
        import importlib
        import auth as auth_mod
        importlib.reload(auth_mod)
        self.auth = auth_mod

    @pytest.mark.asyncio
    async def test_no_key_required(self):
        req = _make_request({"X-Tenant-ID": "dev-tenant"})
        ctx = await self.auth.require_auth(req, api_key=None, bearer=None)
        assert ctx.tenant_id == "dev-tenant"


# ── Tenant allowlist ──────────────────────────────────────────────────────────

class TestTenantAllowlist:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "api_key")
        monkeypatch.setenv("API_KEYS", "key-123")
        monkeypatch.setenv("ALLOWED_TENANTS", "acme,globex")
        import importlib
        import auth as auth_mod
        importlib.reload(auth_mod)
        self.auth = auth_mod

    @pytest.mark.asyncio
    async def test_allowed_tenant_passes(self):
        req = _make_request({"X-Tenant-ID": "acme"})
        ctx = await self.auth.require_auth(req, api_key="key-123", bearer=None)
        assert ctx.tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_disallowed_tenant_raises_403(self):
        from fastapi import HTTPException
        req = _make_request({"X-Tenant-ID": "evil-corp"})
        with pytest.raises(HTTPException) as exc_info:
            await self.auth.require_auth(req, api_key="key-123", bearer=None)
        assert exc_info.value.status_code == 403
