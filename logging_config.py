"""
Structured JSON logging for the MCP Data Agents system.

In development (LOG_FORMAT=pretty):  colourised human-readable output
In production / containers (default): one JSON object per line → CloudWatch / ELK

Usage:
    from logging_config import get_logger
    log = get_logger(__name__)

    log.info("query.received", tenant_id=tid, question_len=len(q))
    log.warning("cache.miss", tenant_id=tid)
    log.error("agent.failed", agent="insight", error=str(exc))

Every log event automatically includes:
    timestamp  — ISO-8601 UTC
    level      — debug / info / warning / error
    logger     — module name passed to get_logger()
    env        — APP_ENV (dev / staging / prod)
"""
import logging
import os
import sys

import structlog

APP_ENV    = os.environ.get("APP_ENV", "dev")
LOG_LEVEL  = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json")   # "json" | "pretty"

# ── Standard library logging → structlog bridge ───────────────────────────────

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)

# ── Shared processors ─────────────────────────────────────────────────────────

_shared_processors = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

# ── Configure structlog ───────────────────────────────────────────────────────

if LOG_FORMAT == "pretty":
    renderer = structlog.dev.ConsoleRenderer(colors=True)
else:
    renderer = structlog.processors.JSONRenderer()

structlog.configure(
    processors=_shared_processors + [renderer],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, LOG_LEVEL, logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    cache_logger_on_first_use=True,
)


# ── Public API ────────────────────────────────────────────────────────────────

def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name).bind(env=APP_ENV)


def bind_request_context(tenant_id: str, user_id: str | None = None) -> None:
    """
    Bind per-request fields into structlog's context vars so every log line
    within the same async task automatically includes them.
    Call at the top of each API request handler.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        tenant_id=tenant_id,
        user_id=user_id or "anonymous",
    )
