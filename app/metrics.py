"""Prometheus metrics. Labels carry only low-cardinality values: never employee IDs,
session IDs or message text."""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter("hr_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_LATENCY = Histogram(
    "hr_http_request_duration_seconds", "HTTP request latency", ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 20, 30, 45),
)
CHAT_ROUTES = Counter("hr_chat_responses_total", "Chat answers by route", ["route"])
CHAT_AGENTS = Counter("hr_chat_agent_total", "Chat answers by the agent that produced them", ["agent"])
TICKET_WRITES = Counter("hr_ticket_writes_total", "Support ticket writes (chat and API)", ["action"])
CHAT_TIMEOUTS = Counter("hr_chat_timeouts_total", "Chat turns that hit the request deadline")
AUTH_FAILURES = Counter("hr_auth_failures_total", "Rejected credentials")
RATE_LIMITED = Counter("hr_rate_limited_total", "Requests rejected by the rate limiter")
READY = Gauge("hr_ready", "1 when ingestion has finished and the service is ready")
RETENTION_PURGED = Counter("hr_retention_purged_total", "Rows removed by the retention job", ["table"])

KNOWN_PATHS = {"/", "/chat", "/health", "/livez", "/readyz", "/metrics"}


def path_label(path: str) -> str:
    if path in KNOWN_PATHS:
        return path
    if path == "/tickets" or path.startswith("/tickets/"):
        return "/tickets"  # ticket IDs never become label values
    return "other"


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
