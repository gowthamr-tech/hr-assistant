"""Production hardening: configuration validation, authentication modes, rate limiting,
HTTP headers, probes, metrics, proposal expiry and data retention."""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings, validate_settings
from app.db import repositories as repo
from app.main import app, initialise, run_retention
from app.security.auth import authenticate
from app.security.ratelimit import RateLimiter
from app.state import STATE
from tests.fakes import ScriptedRouter, make_client

SECRET = "s" * 40
GATEWAY = "g" * 40
OK_BODY = {"session_id": "prod-1", "message": "hi"}


def _token(sub="E201", secret=SECRET, exp_s=300, **claims) -> str:
    payload = {"sub": sub, "exp": int(time.time()) + exp_s, **claims}
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def serve(settings, kb):
    """Start the app with settings overrides and return a TestClient."""
    def make(**overrides) -> TestClient:
        llm, _ = make_client(ScriptedRouter({}))
        initialise(replace(settings, **overrides), llm=llm, kb=kb)
        assert STATE.ready
        return TestClient(app)

    yield make
    STATE.ready = False


# ------------------------------------------------------------------ configuration
def _env(monkeypatch, **values):
    for key in ("APP_ENV", "AUTH_MODE", "AUTH_JWT_SECRET", "AUTH_GATEWAY_TOKEN", "AUTH_JWT_ALGORITHMS",
                "AUTH_JWT_PUBLIC_KEY", "APP_TODAY", "LLM_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_FEDERATION_RULE_ID", "CORS_ALLOW_ORIGINS", "UI_ENABLED", "RATE_LIMIT_PER_MINUTE",
                "LLM_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_development_defaults_keep_original_behaviour(monkeypatch):
    _env(monkeypatch)
    s = load_settings()
    assert s.app_env == "development" and s.auth_mode == "header" and s.ui_enabled


@pytest.mark.parametrize("env, message", [
    ({"APP_ENV": "production"}, "AUTH_MODE=header"),
    ({"APP_ENV": "production", "AUTH_MODE": "jwt", "AUTH_JWT_SECRET": SECRET}, "API key"),
    ({"APP_ENV": "production", "AUTH_MODE": "jwt", "AUTH_JWT_SECRET": SECRET, "ANTHROPIC_API_KEY": "k",
      "APP_TODAY": "2026-10-16"}, "APP_TODAY"),
    ({"APP_ENV": "production", "AUTH_MODE": "jwt", "AUTH_JWT_SECRET": SECRET, "ANTHROPIC_API_KEY": "k",
      "CORS_ALLOW_ORIGINS": "*"}, "CORS"),
    ({"AUTH_MODE": "jwt", "AUTH_JWT_SECRET": "short"}, "AUTH_JWT_SECRET"),
    ({"AUTH_MODE": "jwt", "AUTH_JWT_ALGORITHMS": "RS256"}, "AUTH_JWT_PUBLIC_KEY"),
    ({"AUTH_MODE": "jwt", "AUTH_JWT_SECRET": SECRET, "AUTH_JWT_ALGORITHMS": "HS256,RS256"}, "mix"),
    ({"AUTH_MODE": "jwt", "AUTH_JWT_SECRET": SECRET, "AUTH_JWT_ALGORITHMS": "none"}, "none"),
    ({"AUTH_MODE": "gateway", "AUTH_GATEWAY_TOKEN": "short"}, "AUTH_GATEWAY_TOKEN"),
    ({"AUTH_MODE": "basic"}, "AUTH_MODE"),
    ({"APP_ENV": "staging"}, "APP_ENV"),
    ({"UI_ENABLED": "maybe"}, "UI_ENABLED"),
    ({"RATE_LIMIT_PER_MINUTE": "-1"}, "RATE_LIMIT_PER_MINUTE"),
    ({"LLM_TIMEOUT_S": "fast"}, "LLM_TIMEOUT_S"),
])
def test_unsafe_or_broken_configuration_fails_fast(monkeypatch, env, message):
    _env(monkeypatch, **env)
    with pytest.raises(ValueError, match=message):
        load_settings()


def test_valid_production_configuration(monkeypatch):
    _env(monkeypatch, APP_ENV="production", AUTH_MODE="jwt", AUTH_JWT_SECRET=SECRET, ANTHROPIC_API_KEY="k")
    s = load_settings()
    assert s.is_production and not s.ui_enabled
    _env(monkeypatch, APP_ENV="production", AUTH_MODE="gateway", AUTH_GATEWAY_TOKEN=GATEWAY,
         ANTHROPIC_AUTH_TOKEN="t")  # the SDK's other credential sources count too
    assert load_settings().auth_mode == "gateway"


# ------------------------------------------------------------------ authentication
def test_header_mode(settings):
    assert authenticate({"x-employee-id": " E201 "}, settings).employee_id == "E201"
    assert authenticate({}, settings).employee_id is None


def test_gateway_mode(settings):
    s = replace(settings, auth_mode="gateway", gateway_token=GATEWAY)
    assert authenticate({"x-employee-id": "E201", "x-gateway-token": GATEWAY}, s).employee_id == "E201"
    assert authenticate({"x-employee-id": "E201", "x-gateway-token": "g" * 39}, s).employee_id is None
    assert authenticate({"x-employee-id": "E201"}, s).employee_id is None


def test_jwt_mode(settings):
    s = replace(settings, auth_mode="jwt", jwt_secret=SECRET, jwt_audience="hr-assistant", jwt_issuer="idp")
    good = _token(aud="hr-assistant", iss="idp")
    assert authenticate({"authorization": f"Bearer {good}"}, s).employee_id == "E201"
    assert authenticate({"authorization": f"bearer {good}", "x-employee-id": "E201"}, s).employee_id == "E201"
    rejected = [
        {"authorization": f"Bearer {good}", "x-employee-id": "E202"},              # header disagrees
        {"x-employee-id": "E201"},                                                # no token
        {"authorization": f"Bearer {_token(aud='hr-assistant', iss='idp', exp_s=-120)}"},  # expired
        {"authorization": f"Bearer {_token(aud='other', iss='idp')}"},            # wrong audience
        {"authorization": f"Bearer {_token(aud='hr-assistant', iss='evil')}"},    # wrong issuer
        {"authorization": f"Bearer {_token(secret='x' * 40, aud='hr-assistant', iss='idp')}"},  # bad signature
        {"authorization": "Bearer " + jwt.encode({"sub": "E201", "exp": int(time.time()) + 60,
                                                  "aud": "hr-assistant", "iss": "idp"}, None, algorithm="none")},
        {"authorization": f"Bearer {jwt.encode({'sub': 'E201', 'aud': 'hr-assistant', 'iss': 'idp'}, SECRET)}"},
    ]
    for headers in rejected:
        assert authenticate(headers, s).employee_id is None, headers


def test_jwt_end_to_end(serve):
    c = serve(auth_mode="jwt", jwt_secret=SECRET)
    r = c.post("/chat", json=OK_BODY, headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text
    # The bare header is no longer enough, and an unknown employee in a valid token is a 401 too.
    assert c.post("/chat", json=OK_BODY, headers={"X-Employee-Id": "E201"}).status_code == 401
    r = c.post("/chat", json={**OK_BODY, "session_id": "prod-2"},
               headers={"Authorization": f"Bearer {_token(sub='E999')}"})
    assert r.status_code == 401 and r.json() == {"detail": "missing or invalid credentials"}


# ------------------------------------------------------------------ rate limiting
def test_rate_limiter_window():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.check("E1") == 0 and limiter.check("E1") == 0
    assert limiter.check("E1") > 0
    assert limiter.check("E2") == 0  # per employee
    now[0] = 60.0
    assert limiter.check("E1") == 0
    assert RateLimiter(0).check("E1") == 0  # disabled


def test_rate_limit_returns_429(serve):
    c = serve(rate_limit_per_minute=2)
    h = {"X-Employee-Id": "E201"}
    assert c.post("/chat", json=OK_BODY, headers=h).status_code == 200
    assert c.post("/chat", json=OK_BODY, headers=h).status_code == 200
    r = c.post("/chat", json=OK_BODY, headers=h)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.post("/chat", json={**OK_BODY, "session_id": "other"},
                  headers={"X-Employee-Id": "E202"}).status_code == 200


# ------------------------------------------------------------------ HTTP surface
def test_security_headers_and_request_id(serve):
    c = serve()
    r = c.post("/chat", json=OK_BODY, headers={"X-Employee-Id": "E201", "X-Request-Id": "trace-12345678"})
    assert r.headers["x-request-id"] == "trace-12345678" == r.json()["request_id"]
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    # An unusable inbound ID (here, with spaces) is replaced, not echoed.
    r = c.get("/health", headers={"X-Request-Id": "bad id"})
    assert r.headers["x-request-id"] != "bad id" and len(r.headers["x-request-id"]) == 36


def test_oversized_body_is_rejected_without_reading_it(serve):
    c = serve()
    big = b'{"session_id": "s", "message": "' + b"x" * 70_000 + b'"}'
    r = c.post("/chat", content=big, headers={"X-Employee-Id": "E201", "Content-Type": "application/json"})
    assert r.status_code == 400


def test_probes_and_metrics(serve):
    c = serve()
    assert c.get("/livez").status_code == 200
    assert c.get("/readyz").json() == {"status": "ok"}
    c.post("/chat", json=OK_BODY, headers={"X-Employee-Id": "E201"})
    text = c.get("/metrics").text
    assert "hr_http_requests_total" in text and "hr_chat_responses_total" in text and "hr_ready 1.0" in text
    assert "E201" not in text  # no personal identifiers in labels
    STATE.failed = True
    try:
        assert c.get("/livez").status_code == 503
    finally:
        STATE.failed = False


def test_ui_and_metrics_can_be_disabled(serve):
    c = serve(ui_enabled=False, metrics_enabled=False)
    assert c.get("/").status_code == 404
    assert c.get("/metrics").status_code == 404


# ------------------------------------------------------------------ proposal expiry and retention
def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(microsecond=0).isoformat()


def test_stale_proposal_is_not_applied(conn):
    repo.set_pending(conn, "s-old", "E201", "apply_leave", {"leave_type": "annual"})
    conn.execute("UPDATE assistant_pending_actions SET created_at = ? WHERE session_id = 's-old'",
                 (_ago(minutes=31),))
    assert repo.take_pending(conn, "s-old", "E201", max_age_minutes=30) is None
    assert conn.execute("SELECT COUNT(*) FROM assistant_pending_actions").fetchone()[0] == 0
    repo.set_pending(conn, "s-new", "E201", "apply_leave", {"leave_type": "annual"})
    assert repo.take_pending(conn, "s-new", "E201", max_age_minutes=30)["tool_name"] == "apply_leave"


def test_retention_purges_old_history_only(conn, settings):
    for sid, age in (("old", {"days": 100}), ("recent", {"days": 1})):
        conn.execute("INSERT INTO assistant_sessions VALUES (?, 'E201', ?)", (sid, _ago(**age)))
        conn.execute("INSERT INTO assistant_turns (session_id, role, content, created_at) VALUES (?, 'user', 'x', ?)",
                     (sid, _ago(**age)))
    repo.set_pending(conn, "recent", "E201", "apply_leave", {})
    conn.execute("INSERT INTO assistant_pending_actions VALUES ('old', 'E201', 'apply_leave', '{}', ?)",
                 (_ago(hours=2),))
    leave_rows = conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0]

    counts = run_retention(replace(settings, retention_days=90, pending_ttl_minutes=30))
    assert counts == {"turns": 1, "sessions": 1, "pending": 1}
    assert [r[0] for r in conn.execute("SELECT session_id FROM assistant_sessions")] == ["recent"]
    assert [r[0] for r in conn.execute("SELECT session_id FROM assistant_pending_actions")] == ["recent"]
    assert conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == leave_rows


def test_retention_zero_keeps_history(conn, settings):
    conn.execute("INSERT INTO assistant_turns (session_id, role, content, created_at) VALUES ('s', 'user', 'x', ?)",
                 (_ago(days=4000),))
    assert run_retention(replace(settings, retention_days=0))["turns"] == 0


def test_validate_settings_accepts_test_settings(settings):
    validate_settings(settings)
