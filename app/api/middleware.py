"""HTTP middleware: request IDs, security headers, metrics and an access log.

Written as plain ASGI (not BaseHTTPMiddleware) so it adds no task or buffering
overhead and works with streaming responses.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from app import metrics

log = logging.getLogger("app.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
# The bundled UI uses inline <script>/<style>; nothing is loaded from elsewhere.
_UI_CSP = (b"content-security-policy",
           b"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
           b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")


def request_id_from(scope) -> str:
    """The request ID chosen by the middleware (or a fresh one outside it)."""
    return scope.get("state", {}).get("request_id") or str(uuid.uuid4())


class RequestContextMiddleware:
    def __init__(self, app, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                inbound = value.decode("latin-1")
                break
        request_id = inbound if _REQUEST_ID_RE.match(inbound) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        method, path = scope["method"], scope["path"]
        label = metrics.path_label(path)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.extend(_SECURITY_HEADERS)
                headers.append(_UI_CSP if path == "/" else _API_CSP)
                if path == "/chat":  # answers contain personal data: never cache them
                    headers.append((b"cache-control", b"no-store"))
                if self.hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            status = status_holder["status"]
            metrics.HTTP_REQUESTS.labels(method, label, str(status)).inc()
            metrics.HTTP_LATENCY.labels(method, label).observe(elapsed)
            if label not in ("/health", "/livez", "/readyz", "/metrics"):
                log.info("http request", extra={"request_id": request_id, "method": method, "path": label,
                                                "status": status, "duration_ms": round(elapsed * 1000)})
