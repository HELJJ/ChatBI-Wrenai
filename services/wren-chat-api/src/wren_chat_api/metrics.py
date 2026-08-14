"""Low-cardinality Prometheus metrics for the chat service.

Labels are restricted to route names, terminal statuses, and stable error
codes. Never label with session IDs, questions, SQL, answers, or database
values.
"""

from __future__ import annotations

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter(
    "wren_chat_requests_total",
    "Chat requests by route and terminal status.",
    ["route", "status"],
)
REQUEST_LATENCY = Histogram(
    "wren_chat_request_seconds",
    "Chat request latency in seconds.",
    ["route"],
)
SQL_ATTEMPTS = Counter(
    "wren_chat_sql_attempts_total",
    "SQL attempts by terminal status.",
    ["status"],
)
TRUNCATED_RESULTS = Counter(
    "wren_chat_truncated_results_total",
    "Truncated SQL results by channel.",
    ["kind"],
)
LEASE_CONFLICTS = Counter(
    "wren_chat_lease_conflicts_total",
    "Requests rejected because the session lease was held.",
)
RECOVERED_WORK = Counter(
    "wren_chat_recovered_total",
    "Interrupted work terminalized by the recovery loop.",
    ["kind"],
)
PERSISTENCE_FAILURES = Counter(
    "wren_chat_persistence_failures_total",
    "Audit persistence failures raised as PersistenceFailed.",
)


def metrics_response() -> Response:
    """Render the Prometheus exposition format for the /metrics route."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
