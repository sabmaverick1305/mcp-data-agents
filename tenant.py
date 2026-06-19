"""Tenant context: extraction from HTTP headers and propagation through the pipeline."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str        = "default"
    user_id:   str | None = None
    team_id:   str | None = None


def get_tenant_from_request(request) -> TenantContext:
    """
    Extract tenant identity from HTTP request headers (unauthenticated path).

    Used only by the SSE streaming endpoint, which cannot use the standard
    FastAPI dependency because it returns a generator.  All other endpoints
    use auth.require_auth(), which validates the API key or JWT *before*
    calling this to extract the TenantContext.
    """
    headers = request.headers
    return TenantContext(
        tenant_id=headers.get("X-Tenant-ID", "default"),
        user_id=headers.get("X-User-ID"),
        team_id=headers.get("X-Team-ID"),
    )


def apply_to_trace(tenant: TenantContext, trace) -> None:
    """Stamp tenant identity onto a QueryTrace so the cost ledger can attribute it."""
    trace.tenant_id = tenant.tenant_id
    trace.user_id   = tenant.user_id
    trace.team_id   = tenant.team_id
