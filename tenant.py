"""
Tenant context — identity extraction from HTTP headers and pipeline propagation.

TenantContext is the lightweight identity carrier that flows from the API boundary
through the entire request pipeline. It contains three fields:
  tenant_id   Namespace key for RAG collections, cache keys, cost ledger, rate limits,
              and audit log. Extracted from X-Tenant-ID header (api_key mode) or the
              "tid" JWT claim (jwt mode). Defaults to "default".
  user_id     Individual user within a tenant. Extracted from X-User-ID header or
              JWT "sub" claim. Used for per-user cost attribution in the ledger.
  team_id     Cost-centre grouping for chargeback reporting. Extracted from X-Team-ID
              header or JWT "team" claim. Drives the /costs/by-team endpoint.

TenantContext is frozen (immutable) to prevent accidental mutation across the pipeline.

Two extraction paths:
  auth.require_auth()            — the primary path. Validates the API key or JWT first,
                                   then calls _tenant_from_headers() or _tenant_from_jwt()
                                   internally. All REST endpoints use this via Depends().
  get_tenant_from_request()      — unauthenticated extraction for the SSE streaming
                                   endpoint, which cannot use FastAPI Depends because it
                                   returns a generator. Auth must be validated externally
                                   before calling this function.

apply_to_trace(tenant, trace):
  Stamps all three identity fields onto a QueryTrace so the cost ledger, Prometheus
  metrics, and Redis audit log can attribute every query to the correct tenant/user/team.
"""
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
