# Deployment

How to run the HR Assistant in production. The development defaults (`docker compose up`) are built for
local testing and are **not safe to expose**: they trust the `X-Employee-Id` header as sent.

## 1. Checklist

| | Setting / action | Why |
|---|---|---|
| ☐ | `APP_ENV=production` | Turns on the start-up safety checks below, HSTS, and turns the test UI off |
| ☐ | `AUTH_MODE=jwt` or `AUTH_MODE=gateway` and its secret | Without it anyone can act as any employee (start-up fails with `header`) |
| ☐ | LLM provider key from your secret manager | Start-up fails without one |
| ☐ | `APP_TODAY` empty | It freezes "today" for every date rule (start-up fails if set) |
| ☐ | TLS-terminating reverse proxy or ingress in front | The service speaks plain HTTP on port 8000 |
| ☐ | Route only `/chat` publicly; keep `/metrics`, `/livez`, `/readyz` internal | Metrics are aggregate-only but still internal detail |
| ☐ | `./data` on persistent storage, owned by uid 10001 (or any non-root user) | `veltrix.db` holds leave data and chat history |
| ☐ | Backups of `veltrix.db` (§6) | Leave requests are written there |
| ☐ | Review `RATE_LIMIT_PER_MINUTE`, `RETENTION_DAYS`, `PENDING_TTL_MINUTES` | Cost control and data-retention policy |
| ☐ | Scrape `/metrics`, ship JSON logs, alert on §5 | Detect outages and abuse |

## 2. Authentication

The employee's role, region and manager always come from `veltrix.db`; authentication only decides which
employee ID a request may act as (`app/security/auth.py`). Every failure returns the same
`401 {"detail": "missing or invalid credentials"}`.

### `AUTH_MODE=jwt` (recommended when clients call the service directly)

Clients send `Authorization: Bearer <token>` issued by your identity provider (Okta, Entra ID, Auth0,
Keycloak, …). The token must carry `exp` and the employee ID in `AUTH_JWT_EMPLOYEE_CLAIM` (default `sub`),
and that ID must exist in `employees`.

```env
AUTH_MODE=jwt
AUTH_JWT_ALGORITHMS=RS256
AUTH_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\nMIIB...\n-----END PUBLIC KEY-----"
AUTH_JWT_ISSUER=https://login.example.com/
AUTH_JWT_AUDIENCE=hr-assistant
AUTH_JWT_EMPLOYEE_CLAIM=employee_id
```

For a shared secret instead, use `AUTH_JWT_ALGORITHMS=HS256` and `AUTH_JWT_SECRET` (at least 32
characters). `none` and mixed HS/RS algorithm lists are rejected at start-up. 30 seconds of clock skew is
allowed. JWKS key rotation is not built in: when your provider rotates keys, update `AUTH_JWT_PUBLIC_KEY`
and restart, or use gateway mode and let the gateway validate tokens.

### `AUTH_MODE=gateway` (behind an SSO proxy or API gateway)

Your gateway authenticates the user, then forwards the request with `X-Employee-Id` set **by the gateway**
(overwriting anything the client sent) and `X-Gateway-Token: <AUTH_GATEWAY_TOKEN>` (at least 32
characters, compared in constant time). The service must not be reachable except through the gateway:
bind it to a private network or `127.0.0.1` (the production compose file does this).

## 3. Running it

### Single host with Docker Compose

```bash
cp .env.example .env     # then set APP_ENV=production, AUTH_*, the LLM key
chown -R 10001:10001 ./data
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`docker-compose.prod.yml` builds the `runtime` stage, publishes the port on `127.0.0.1` only (put nginx,
Caddy or Traefik in front for TLS), drops all Linux capabilities except the two the entrypoint needs to
switch user, sets `no-new-privileges`, limits memory, CPU and PIDs, and rotates container logs.

### Kubernetes (outline)

- **Image:** build `--target runtime`; it contains no tests, pytest or ruff.
- **Replicas: 1**, strategy `Recreate` (see §7). Data on a `ReadWriteOnce` PersistentVolume mounted at
  `/app/data`.
- **Security context:** `runAsUser: 10001`, `runAsGroup: 10001`, `fsGroup: 10001`, `runAsNonRoot: true`,
  `allowPrivilegeEscalation: false`, `capabilities: {drop: [ALL]}`. Started as non-root, the entrypoint
  runs the server directly.
- **Probes:** `startupProbe` on `/readyz` (allow ~2 minutes: ingestion runs OCR), `readinessProbe` on
  `/readyz`, `livenessProbe` on `/livez`. `/livez` returns 503 only if start-up failed, so a broken
  configuration or corpus restarts the pod instead of leaving it unready forever.
- **Shutdown:** `terminationGracePeriodSeconds: 40`. On SIGTERM uvicorn stops accepting connections and
  waits up to 30 s for in-flight turns.
- **Resources:** start with a 2 GB memory limit (the compose overlay's value) and 1–2 CPUs, then size
  from the container's observed peak during ingestion; the embedding model and index dominate.
- **Secrets:** `AUTH_JWT_SECRET` / `AUTH_GATEWAY_TOKEN` and the LLM key from a Secret, not a ConfigMap.

## 4. Configuration reference (production-relevant)

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `production` enables the checks in §1 |
| `AUTH_MODE` | `header` | `header` \| `gateway` \| `jwt` |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per employee, per process; `0` disables. Each turn can make up to 3 LLM calls |
| `CORS_ALLOW_ORIGINS` | empty | Comma-separated origins for a browser front end on another origin. `*` is refused in production |
| `UI_ENABLED` | on in dev, off in prod | The test page only works with `AUTH_MODE=header` |
| `METRICS_ENABLED` | `true` | Prometheus `/metrics` |
| `RETENTION_DAYS` | `90` | Chat turns and idle sessions older than this are deleted every 6 h; `0` keeps them |
| `PENDING_TTL_MINUTES` | `30` | An unconfirmed leave proposal expires after this |
| `LOG_FORMAT` | `json` in the image | One JSON object per line; personal data is scrubbed (SECURITY_AND_PRIVACY.md §1.12) |

All variables are in `.env.example`. A malformed value (`UI_ENABLED=maybe`, `LLM_TIMEOUT_S=fast`) fails
start-up with a message naming the variable.

## 5. Monitoring

Metrics at `/metrics` (no personal identifiers in labels):

| Alert | Expression (PromQL sketch) |
|---|---|
| Not ready | `hr_ready == 0` for 5 m |
| Error rate | `sum(rate(hr_http_requests_total{status=~"5.."}[5m])) > 0` |
| LLM degraded | `rate(hr_chat_timeouts_total[10m]) > 0`, or a jump in `hr_chat_responses_total{route="clarify"}` |
| Slow answers | `histogram_quantile(0.95, sum by (le) (rate(hr_http_request_duration_seconds_bucket{path="/chat"}[5m]))) > 20` |
| Abuse / misconfigured client | `rate(hr_auth_failures_total[5m])` or `rate(hr_rate_limited_total[5m])` above baseline |

Logs are JSON on stdout. Every line for a request carries its `request_id`, which is also returned in the
`X-Request-Id` header, so a user's report can be traced without logging message text.

## 6. Backups and data

`veltrix.db` holds the leave tables and the `assistant_*` tables (sessions, recent chat turns, pending
proposals, extracted holidays). Back it up with SQLite's online backup, which is safe while the service runs:

```bash
docker compose exec assistant python -c "import sqlite3, datetime; \
src = sqlite3.connect('/app/data/veltrix.db'); \
src.backup(sqlite3.connect(f'/app/data/backup-{datetime.date.today()}.db'))"
```

Then move the file off the host. (The image has no `sqlite3` CLI; on a host that has one,
`sqlite3 veltrix.db ".backup 'copy.db'"` does the same.)

Do not copy the file with `cp` while the service is writing. Holidays and the search index are rebuilt
from the corpus at every start, so the corpus folder and `manifest.json` only need versioning, not backup.

## 7. Scaling

Run **exactly one replica**. The search index lives in process memory (rebuilt in ~10–60 s at start), and
SQLite allows one writer on a local disk; replicas with their own copies of `veltrix.db` would disagree
about leave balances, and SQLite on a network file system is unsafe. Within one replica, up to 32 chat
turns run concurrently; the bottleneck is LLM latency, not CPU.

To scale out, move the leave and `assistant_*` tables to PostgreSQL (all SQL is in
`app/db/repositories.py`), move rate limiting to the gateway or Redis, and persist the index as described
in DESIGN.md §7.

## 8. Upgrades and rollback

The service adds tables (`assistant_*`) and indexes with `CREATE … IF NOT EXISTS` and never alters the
leave tables, so a rollback to an older image keeps working with the same database. Take a backup (§6)
before upgrading anyway.
