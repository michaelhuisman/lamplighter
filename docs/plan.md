# lamplighter — design and phasing

## Goal

Run Ansible playbooks from Git on a schedule and on demand, with live logs, run history,
secrets from OpenBao and authentication via Keycloak. For now it runs with Docker Compose
on a single LXC/VM, but it is set up so that multiple workers and scheduler replicas can
be added later without a refactor.

## Architecture

| Role        | Responsibility                                                              |
|-------------|-----------------------------------------------------------------------------|
| `api`       | REST API (FastAPI), CRUD on configuration, starting runs, logs via SSE      |
| `scheduler` | Evaluate schedules (APScheduler) and put runs in `queued`                   |
| `worker`    | Claim runs, check out the repo, run ansible-runner, store events            |
| `migrate`   | One-shot `alembic upgrade head` before the other services start             |

The scheduler executes nothing; it only creates run records. Postgres is the only state
store: jobstore, queue and locks.

### Leader election

On startup the scheduler takes `pg_try_advisory_lock(0x5343, 1)` on a dedicated,
long-lived connection. Only the lock holder starts APScheduler. The others retry every
10 seconds (`LAMPLIGHTER_SCHEDULER_LOCK_RETRY_S`). The same connection does
`LISTEN schedules_changed` and serves as health check. If the connection drops,
APScheduler stops immediately and the replica returns to follower mode.

Postgres uses short TCP keepalives. A frozen leader (one that does not crash) therefore
loses the lock within about 15 seconds.

Guard: `runs(schedule_id, scheduled_for)` is unique and inserts use
`ON CONFLICT DO NOTHING`. Even if two replicas briefly both think they are leader, each
firing produces only one run.

### Schedules → APScheduler jobs

The `schedules` table is the source of truth and the jobs are derived from it. The leader
reconciles after every `NOTIFY schedules_changed` (which the API sends after every change)
and also every `LAMPLIGHTER_SCHEDULER_SYNC_INTERVAL_S` (5s):

- job id `schedule:<id>`, job name = fingerprint of cron, timezone and misfire_grace_s;
- new or changed: `add_job(replace_existing=True)`. Unchanged jobs are kept, so their
  `next_run_time` is preserved (needed for misfire detection after a failover);
- disabled or deleted: `remove_job`.

The job derives `scheduled_for` from the trigger: the last fire time ≤ now. That is
deterministic, so two leaders arrive at the same value. `coalesce=True`: after downtime a
job fires once. A firing outside `misfire_grace_s` produces a `skipped` run with reason
`missed: scheduler unavailable`.

The `apscheduler_jobs` table is created via Alembic. The jobstore itself does
`create(checkfirst=True)`, which is then a no-op.

### Cron semantics

Cron expressions have 5 fields and are interpreted like Vixie cron:

- weekday 0 and 7 = Sunday, 1 = Monday. APScheduler's `from_crontab` uses 0 = Monday,
  so we translate the field to names;
- restricting both day of month and day of week is rejected. Cron combines them with OR,
  APScheduler with AND;
- DST, hour field with wildcard or step (`*`, `*/2`): the job fires every real hour, so
  twice in the repeated autumn hour;
- DST, fixed hour (`30 2 * * *`): the job fires once a day. In spring a non-existent time
  fires right after the jump.

### Queue

The worker claims via:

```sql
SELECT id FROM runs
WHERE status = 'queued'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

The status then goes to `running` with `worker_id` and `started_at`. Polling happens at
an interval (default 2s), possibly supplemented with `LISTEN/NOTIFY` later.

### Overlap

Per template the worker holds a session lock during the run:
`pg_try_advisory_lock(0x5450, template_id)` on a dedicated connection. A transaction lock
is not enough, because a run spans several transactions. The policy is copied to
`runs.overlap_policy` when the run is created:

- `skip`: the new run gets status `skipped` with a reason. The scheduler already checks
  this on creation (template lock in `pg_locks` or an earlier run in the queue). The
  worker checks again when claiming.
- `queue`: the run stays `queued` until the lock is free. The claim skips it and takes
  the next candidate, so other templates are not blocked.
- Manual runs behave as `queue`.

The leader lock and template locks use the form with two int4 values (namespace, id):
`0x5343` for the scheduler and `0x5450` for templates.

## Data model

**projects**
id, name, git_url, branch, credential_id (nullable), created_at, updated_at

**inventories**
id, name, project_id (nullable), source_type (`project_file` | `inline`),
path (for project_file), content (for inline), created_at, updated_at

**credentials**
id, name, type (`ssh_key` | `vault_password` | `git_token` | `known_hosts`), openbao_path,
openbao_key, created_at. References only, never secret values.

**templates**
id, name, project_id, playbook_path, inventory_id, extra_vars (jsonb), limit, tags,
skip_tags, verbosity, machine_credential_id, vault_credential_id (nullable),
known_hosts_credential_id (nullable), timeout_s, created_at, updated_at

**schedules**
id, template_id, cron, timezone, enabled, overlap_policy (`skip` | `queue`),
misfire_grace_s (default 60), extra_vars_override (jsonb), created_at, updated_at

**runs**
id, template_id, schedule_id (nullable, `ON DELETE SET NULL`), scheduled_for (nullable),
overlap_policy, triggered_by (`schedule` | `user:<sub>`),
status, extra_vars (jsonb, effective), limit (effective), commit_sha, worker_id,
created_at, started_at, finished_at, cancel_requested_at, rc, status_reason,
stats (jsonb: ok/changed/failed/unreachable/skipped/rescued/ignored per host)

`extra_vars` and `limit` are the effective launch parameters (template merged with the
overrides from `/launch`). `cancel_requested_at` is set by `/cancel` on a running run;
the worker picks that up via `cancel_callback`.

**run_events**
id (bigserial), run_id, seq, event, host, task, created_at, stdout, data (jsonb, filtered)

**users**
id, source (`local` | `oidc`), username (for OIDC: `sub`), display_name, email,
password_hash (argon2id, local only), roles (local only), disabled,
failed_logins, locked_until, created_at, last_login_at

**sessions** (UI)
id, token_hash (sha256 of the cookie id), user_id, csrf_token, oidc_roles (snapshot at
login), created_at, last_seen_at, expires_at

**api_tokens** (local users)
id, user_id, name, token_hash (sha256), prefix, created_at, expires_at, last_used_at,
revoked_at

**audit_log**
id, at, actor (`user:local:<name>` | `user:oidc:<sub>` | `cli`), action, object_type,
object_id, details (jsonb: field names, no values or secrets), ip

**notifications** (outbox for webhooks)
id, run_id, target (fingerprint of the webhook URL, never the URL itself), event,
status (`pending` | `sent` | `failed`), attempts, next_attempt_at, last_error,
created_at, sent_at

**run_stats_archive** (counts of runs removed by retention)
template, status, runs, duration_count, duration_sum, duration_buckets (jsonb)

### Run statuses

`queued` → `running` → `successful` | `failed` | `error` | `timeout` | `canceled`
`queued` → `skipped` | `canceled`

## Executing a run

1. Claim the run (see Queue) and take the overlap lock.
2. Clone or fetch the repo into the cache (`LAMPLIGHTER_REPO_CACHE_DIR/<project_id>`),
   check out the branch HEAD in a per-run worktree and store `commit_sha`.
3. Create the private data dir under `LAMPLIGHTER_RUNTIME_DIR/<run_id>`. The worktree is
   in `<run_id>/project`, an inline inventory in `<run_id>/inventory/hosts`. No `env/`
   files are written (`suppress_env_files`): extravars are passed as an argument, the SSH
   key goes via ansible-runner's FIFO to ssh-agent, and a vault password as
   `--vault-password-file` (0600) in the private data dir.
4. `ansible_runner.run(..., event_handler=..., cancel_callback=..., timeout=...)` with
   `process_isolation=False`.
5. The event_handler filters and writes to `run_events` in batches (50 events or 1s). It
   returns `False`, so ansible-runner does not write the unfiltered events to disk. The
   stats come from the `playbook_on_stats` event.
6. Finish: status, rc, stats and `finished_at`. Remove the worktree and the private data
   dir in `finally`.
7. On SIGTERM the worker stops claiming new runs and lets the current run finish within
   the grace period.

## API (v1)

```
GET/POST        /api/v1/projects
GET/PUT/DELETE  /api/v1/projects/{id}
(same CRUD for inventories, credentials, templates, schedules;
 a schedule also has a computed field `next_run_at`)

POST  /api/v1/templates/{id}/launch     body: extra_vars, limit (optional)
GET   /api/v1/runs                      filters: template_id, status, since
GET   /api/v1/runs/{id}
GET   /api/v1/runs/{id}/events          paginated
GET   /api/v1/runs/{id}/stream          SSE, live events
POST  /api/v1/runs/{id}/cancel
POST  /api/v1/runs/{id}/relaunch        body: failed_hosts_only (optional)

GET   /healthz                          liveness
GET   /readyz                           db connection
GET   /api/v1/me                        own identity and roles
GET/POST/DELETE /api/v1/tokens          own API tokens (local users)
GET/POST/PATCH  /api/v1/users           user management (admin)
PUT   /api/v1/users/{id}/password       reset password (admin)
GET   /api/v1/audit                     audit log (admin)

GET   /metrics                          Prometheus (optional scrape token since 5a)

GET   /ui/...                           server-rendered UI (Jinja2 + htmx)
```

## Configuration (env, prefix `LAMPLIGHTER_`)

| Variable                                | Default                                  |
|-----------------------------------------|------------------------------------------|
| `LAMPLIGHTER_DATABASE_URL`              | —                                        |
| `LAMPLIGHTER_RUNTIME_DIR`               | `/run/lamplighter`                       |
| `LAMPLIGHTER_REPO_CACHE_DIR`            | `/var/cache/lamplighter/repos`           |
| `LAMPLIGHTER_WORKER_ID`                 | hostname                                 |
| `LAMPLIGHTER_POLL_INTERVAL_S`           | `2`                                      |
| `LAMPLIGHTER_LOG_LEVEL`                 | `INFO`                                   |
| `LAMPLIGHTER_API_HOST`                  | `0.0.0.0`                                |
| `LAMPLIGHTER_API_PORT`                  | `8000`                                   |
| `LAMPLIGHTER_ANSIBLE_HOST_KEY_CHECKING` | `true`                                   |
| `LAMPLIGHTER_SCHEDULER_LOCK_RETRY_S`    | `10`                                     |
| `LAMPLIGHTER_SCHEDULER_SYNC_INTERVAL_S` | `5`                                      |
| `LAMPLIGHTER_PUBLIC_URL`                | `http://localhost:8000`                  |
| `LAMPLIGHTER_WEBHOOK_OPENBAO_PATH`      | — (empty = no webhooks)                  |
| `LAMPLIGHTER_WEBHOOK_CACHE_S`           | `60`                                     |
| `LAMPLIGHTER_AUTH_LOCAL_ENABLED`        | `true`                                   |
| `LAMPLIGHTER_SESSION_COOKIE_SECURE`     | `true` (dev: `false`)                    |
| `LAMPLIGHTER_SESSION_IDLE_S`            | `28800` (8 hours)                        |
| `LAMPLIGHTER_SESSION_MAX_S`             | `86400` (24 hours)                       |
| `LAMPLIGHTER_LOGIN_MAX_FAILURES`        | `5`                                      |
| `LAMPLIGHTER_LOGIN_LOCKOUT_S`           | `900`                                    |
| `LAMPLIGHTER_OIDC_ISSUER`               | — (empty = local login only)             |
| `LAMPLIGHTER_OIDC_DISCOVERY_URL`        | — (default: from issuer)                 |
| `LAMPLIGHTER_OIDC_CLIENT_ID`            | `lamplighter`                            |
| `LAMPLIGHTER_OIDC_CLIENT_SECRET`        | —                                        |
| `LAMPLIGHTER_OIDC_AUDIENCE`             | — (default: client id)                   |
| `LAMPLIGHTER_OPENBAO_ADDR`              | — (worker and scheduler)                 |
| `LAMPLIGHTER_OPENBAO_ROLE_ID`           | — (AppRole per role)                     |
| `LAMPLIGHTER_OPENBAO_SECRET_ID`         | —                                        |
| `LAMPLIGHTER_OPENBAO_KV_MOUNT`          | `secret`                                 |
| `LAMPLIGHTER_OPENBAO_CA_CERT`           | — (system CAs)                           |
| `LAMPLIGHTER_TRUSTED_PROXIES`           | `127.0.0.1` (IPs/CIDRs, comma-separated) |
| `LAMPLIGHTER_METRICS_TOKEN`             | — (empty = `/metrics` open)              |
| `LAMPLIGHTER_RETENTION_EVENTS_DAYS`     | `30` (0 = never)                         |
| `LAMPLIGHTER_RETENTION_RUNS_DAYS`       | `180` (0 = never)                        |
| `LAMPLIGHTER_RETENTION_AUDIT_DAYS`      | `365` (0 = never)                        |
| `LAMPLIGHTER_RETENTION_TOKENS_DAYS`     | `30` (0 = never)                         |
| `LAMPLIGHTER_RETENTION_HOUR_UTC`        | `3`                                      |
| `LAMPLIGHTER_REAPER_GRACE_S`            | `120`                                    |
| `LAMPLIGHTER_REAPER_INTERVAL_S`         | `60`                                     |

## Dev environment (`compose.dev.yml`)

- `postgres`: Postgres 18 (image `postgres:18.6`). The volume belongs on
  `/var/lib/postgresql`; from 18 on PGDATA is `/var/lib/postgresql/18/docker`. An upgrade
  from an older major version goes via `pg_dump`/restore or `pg_upgrade`, not by just
  swapping the image.
- `migrate`, `api`, `scheduler`, `worker`: built from the local Dockerfile.
- `ssh-target`: Debian container with sshd and python3, key auth with a generated dev
  key. This is the only target for integration tests.
- `tests/fixtures/repo/`: test playbooks as plain files (ping, placing a file, a
  deliberately failing task, a long sleep for timeout and cancel, no_log). The init
  container `fixture-repo` builds a bare repo from them in the `fixtures` volume
  (`file:///fixtures/repo.git`).
- `keycloak`, `openbao` + `openbao-init`, `git-http` and `webhook-sink`: see phases 3
  and 4.
- `dev`: tooling container (profile `tools`) with ruff, mypy and pytest. It has the
  source code mounted and runs in the compose network.
- The runtime dir is a named tmpfs volume, shared between workers and read-only with
  `dev` (for the cleanup tests).
- `scripts/dev-keys.sh` generates all dev secrets (SSH key, vault password, Keycloak and
  OpenBao secrets, git token, HMAC and metrics token) in `.dev/`.

---

## Phase 1 — MVP: manual runs

**Scope:** project structure, config, db, models and migrations for projects,
inventories, credentials (dev: local key reference), templates, runs and run_events.
Worker with ansible-runner, API for CRUD, launch, runs and events. Compose files and
Dockerfile. No auth, schedules or OpenBao yet.

**Acceptance criteria**
- `docker compose -f compose.dev.yml up` starts all services and `migrate` succeeds.
- Launching a template against `ssh-target` via `POST /launch` produces a run with
  status `successful`, a correct `commit_sha`, stats and events.
- The failing playbook gives `failed` with rc ≠ 0.
- Timeout and cancel work and give `timeout` and `canceled`.
- The private data dir no longer exists afterwards, not even after an exception.
- Two workers never claim the same run (integration test with `--scale worker=2`).
- ruff, mypy and pytest are green.

## Phase 2 — Scheduling

**Scope:** schedules model and API, APScheduler with Postgres jobstore, leader election,
synchronisation between the schedules table and APScheduler jobs (create, update,
delete, enable), overlap policies and misfire handling.

**Acceptance criteria**
- A schedule with cron `* * * * *` produces exactly one run every minute, also with
  `--scale scheduler=2`.
- Kill the leader: another replica takes over within 30s, without duplicate runs.
- `skip` gives a run with status `skipped` if the previous one is still running. `queue`
  waits.
- A change to a schedule via the API takes effect without a restart.
- DST transition: a unit test with time zone `Europe/Amsterdam` fires correctly.

## Phase 3 — UI and observability

_Order swapped compared to the original plan: the UI first, because for now it only runs
in dev. Auth seams (`current_user`) are in place from phase 3._

**Scope:** a frontend (runs overview, live log view, managing templates and schedules),
`/metrics` with Prometheus (run duration histogram, runs per status per template, queue
depth, last successful run per schedule) and webhook notifications on `failed`, `error`
and `timeout`.

**Details**
- **UI:** English, server-rendered with Jinja2 and htmx (2.0.11, vendored in
  `app/ui/static`, no Node toolchain). Under `/ui`. Forms validate with the same pydantic
  schemas as the API.
- **Management in the UI:** runs, templates, schedules, projects, inventories and
  credentials (only references to OpenBao; the UI never asks for secret values). For
  admins also users and the audit log.
- **SSE:** `GET /api/v1/runs/{id}/stream` sends `run_event` (id = seq), `status` and
  `end`. The server polls `run_events` every 0.5s. Reconnecting resumes from
  `Last-Event-ID`. In the UI via `EventSource`; in phase 4 that works with a session
  cookie without changes.
- **Metrics:** a custom collector computes everything from Postgres on every scrape. That
  way the values are the same across api, scheduler and worker replicas and always
  current.
- **Webhooks:** an outbox table `notifications`, filled in the same transaction that
  finishes the run. The scheduler leader sends every 5s (`SKIP LOCKED`) with backoff
  5s → 10s → … max 10 min, and gives up after 10 attempts (`failed`). Optionally an
  HMAC-SHA256 signature in `X-Lamplighter-Signature`.
- **Auth seam:** in phase 3 `current_user()` returns an anonymous admin. Routes and
  templates check `Principal.can(action)`.

**Acceptance criteria**
- The live log in the UI follows a running run via SSE.
- The metrics are scrapeable and are updated on status changes.
- The webhook sends JSON with run id, template, status and a link. Retry with backoff.

## Phase 4 — Secrets and auth

_Delivered in two parts: **4a** auth (local users, API tokens, Keycloak, RBAC, audit,
CSRF) and **4b** OpenBao (credentials, git tokens, webhook URLs)._

**Details 4a**
- **Identities:** local users (roles in the DB, argon2id, lockout after 5 failures) and
  Keycloak users (roles from the token, client roles of `lamplighter`; created on first
  login). Both sources can be turned on and off separately.
  `triggered_by` is `user:local:<name>` or `user:oidc:<sub>`.
- **UI sessions:** a server-side session in the DB with a cookie (`HttpOnly`,
  `SameSite=Lax`, `Secure`), an idle timeout of 8 hours and a maximum of 24 hours. CSRF
  uses a token per session (form field or `X-CSRF-Token`) plus an Origin check.
  `EventSource` sends the cookie along.
- **OIDC login:** authorization code with PKCE, server-side (confidential client).
  `state`, `nonce` and the verifier live in a short-lived cookie. The id_token and the
  access token are validated via JWKS. A login without a role is refused.
- **API:** accepts the session cookie (with CSRF), a local API token
  (`Bearer lamplighter_…`, only the hash in the DB, with an expiry date) or a Keycloak
  JWT (`iss`, `aud` and `exp` checked). Without valid authentication the result is 401,
  without the right role 403.
- **Roles:** `viewer` (read), `operator` (plus launch and cancel), `admin` (plus
  configuration, user management and audit).
- **Audit:** recorded in the same transaction as the change.
- **CLI:** `python -m app create-user <name> --role admin` (password via prompt or
  stdin) and `python -m app create-token <name> --name <x>`.
- **Dev:** Keycloak 26.7.4 with a realm import. The client secret and the test passwords
  are generated in `.dev/` by `scripts/dev-keys.sh`.

**Details 4b**
- **OpenBao client** (hvac): AppRole login. Before every read the token is renewed once
  less than a third of the TTL remains. If renewal fails, or the max TTL is reached, a new
  login follows; on a 403 it retries once. Reads use KV v2. Error messages contain only
  path and key.
- **Path convention and policies** (least privilege):

  | Path (mount `secret`) | Content | Readable by |
  |---|---|---|
  | `ssh/<name>` | SSH key under `openbao_key`, or `known_hosts` | worker |
  | `vault/<name>` | vault password under `openbao_key` | worker |
  | `git/<name>` | token under `openbao_key`, optional `username` (default `x-access-token`) | worker |
  | `webhooks/<name>` | `urls` (JSON list), optional `hmac_secret` | scheduler |

  The API reads no secrets.
- **Git via https:** `projects.credential_id` refers to a `git_token` credential. Clone
  and fetch go through an askpass script with files (0600) in the private data dir. The
  token never ends up in the URL, the process arguments, the environment or the config
  of the cache repo, and `credential.helper` is empty.
- **Webhooks:** on an error status the worker inserts a single outbox row with
  `target='*'` and knows no webhook secrets. The scheduler leader reads the config from
  OpenBao (cached for `LAMPLIGHTER_WEBHOOK_CACHE_S`) and expands `*` into one row per URL
  fingerprint. If OpenBao is unreachable, the `*` row stays.
- **Dev:** OpenBao 2.7.0 in dev mode with `openbao-init` (policies, AppRoles with TTL 60s
  and max 300s, dev secrets). Plus a `git-http` container (git http-backend with basic
  auth). `scripts/it-secret-scan.sh` searches the container logs for all dev secrets.

**Scope:** an OpenBao client (AppRole login, token renew), credential resolution in the
worker, Keycloak OIDC validation (JWT via JWKS) and RBAC on client roles `viewer`,
`operator` and `admin`. An audit log table and `triggered_by` with the subject.

**Acceptance criteria**
- The worker fetches the SSH key and vault password just in time. None of it ends up in
  the db, logs or events (tested with a known secret string).
- `viewer` can only read, `operator` can launch and cancel, `admin` can change
  configuration.
- An expired or invalid token gives 401, a missing role 403.
- In dev: OpenBao in dev mode and a Keycloak container in `compose.dev.yml`.

## Phase 5 — Hardening and production

**Scope:** a production compose file, a CI pipeline (lint, test, build, push to a
registry), an Ansible role `deploy/roles/lamplighter`, a retention job for `run_events`
and old runs, a Postgres backup (`pg_dump`) and TLS via a reverse proxy.

_Delivered in parts: **5a** hardening of the app; **5b** production and deploy, first
the CI (5b-1) and then the Ansible role, the production compose and the backup (5b-2)._

**Choices 5b**
- **Target platform:** containers via Docker Compose on a VM. The OS does not matter;
  Docker Engine with the compose plugin is the standard. A Helm chart follows in phase 6.
- **TLS:** the existing nginx on the system terminates TLS; the app itself does no TLS.
  An example config goes in `docs/deploy/nginx.conf`, with buffering off for SSE, the
  `X-Forwarded-*` headers, a rate limit on the login and `/metrics` restricted to
  monitoring. `LAMPLIGHTER_TRUSTED_PROXIES` points to the address nginx connects from;
  with Docker port forwarding that is usually the bridge gateway, not `127.0.0.1`.
- **Postgres:** in the compose on the same host.
- **Registry:** GHCR (`ghcr.io/<owner>/lamplighter`).

**Details 5b-1 (CI)**, GitHub Actions in `.github/workflows/ci.yml`, with the actions
pinned to a commit SHA:
- `lint`: ruff and mypy.
- `unit`: pytest `tests/unit`.
- `integration`: `compose.dev.yml` with Docker on the runner, with `dev-keys.sh`, the
  integration tests, the slow tests, `it-failover.sh` and `it-secret-scan.sh`. On a
  failure the container logs are kept as an artifact.
- `image`: only on a push to `main` or a `v*` tag, and only after green tests. Builds
  target `runtime` and pushes to GHCR tagged with the full git SHA, `latest` (on `main`)
  and the semver (on a tag).
- The host scripts pick the runtime via `$CONTAINER` (Podman by default if available,
  otherwise Docker; see `scripts/lib.sh`).

**Details 5b-2 (deploy)**
- **Ansible role** `deploy/roles/lamplighter` (+ `deploy/site.yml`, `deploy/restore.yml`
  and an example inventory). Requires Docker Engine with the compose plugin and systemd;
  it does not install Docker. Everything lives in `/opt/lamplighter`: `compose.yml` (from
  the role template; the old root `compose.yml` is gone), `secrets/*.env` (0600) and
  `backups/`. Secrets come from ansible-vault; the database password reaches the app as
  `PGPASSWORD`, so it is not in the database URL. Argument specs validate the variables.
- **Order:** pull, Postgres, `migrate` (reports `changed` only when the schema changed:
  "schema upgraded"), api and scheduler, then `worker-a` and `worker-b` one at a time,
  and wait for `/readyz`. A second run gives `changed=0`.
- **Update without interruption:** two worker services instead of one scaled service. A
  worker that gets SIGTERM claims nothing new and finishes its run
  (`stop_grace_period`, default 1h); the other worker keeps going. Migrations must
  therefore stay compatible with the previous version.
- **Network:** a fixed subnet for the compose network, so the proxy address is fixed:
  `LAMPLIGHTER_TRUSTED_PROXIES` defaults to its gateway (172.30.117.1).
- **Backup:** a systemd timer (`lamplighter-backup.timer`, daily, `Persistent=true`)
  runs the one-shot compose service `backup` (Postgres image, profile `backup`):
  `pg_dump -Fc`, pruning after `lamplighter_backup_keep_days`, and an optional host
  command afterwards (`lamplighter_backup_post_command`, e.g. an offsite copy).
- **Maintenance status:** the table `maintenance_status` (one row per task: last attempt,
  status, last success, error) is written by the retention job and by the backup (psql).
  Metrics: `lamplighter_maintenance_last_success_timestamp_seconds{task}` and
  `lamplighter_maintenance_last_attempt_failed{task}`.
- **Restore:** `restore.yml` makes a safety backup, stops the app, replaces the `public`
  schema with the dump, runs the migrations and starts everything again. See
  `docs/deploy/restore.md`.
- **nginx:** an example in `docs/deploy/nginx.conf` (TLS, SSE without buffering,
  `X-Forwarded-*`, a rate limit on the login, `/metrics` only from monitoring).
- **CI:** `lint-deploy` (ansible-lint, profile `production`, and a syntax check);
  `deploy` runs `deploy/tests/deploy-test.sh` on the runner: install, idempotence, an
  image update during a running run, a backup via the systemd service and a restore. The
  test fixtures (dev OpenBao, ssh-target, git-http) come from `compose.dev.yml` as
  project `lamplighter-fixtures`. `scan`: Trivy (pinned by digest) on the runtime image;
  fixable HIGH/CRITICAL findings fail the build (exceptions in `.trivyignore`), and all
  findings go to the Security tab as SARIF on pushes.
- **README:** a description of the app with the logo, and the installation steps.

**Details 5a**
- **Retention:** a daily internal job of the scheduler leader
  (`LAMPLIGHTER_RETENTION_HOUR_UTC`), with its own advisory lock. What can be removed:
  - events of finished runs
  - finished runs, cascading to events and notifications
  - the audit log
  - revoked or expired API tokens

  All periods are configurable, and `0` means never. Deletion happens in batches of
  5,000. Running and queued runs are always kept.
- **Metrics after retention:** before deleting, the counts per template and status (runs,
  duration, buckets) are added to `run_stats_archive`, in the same transaction.
  `lamplighter_runs_total` and the duration histogram are live plus archive, so
  Prometheus sees no counter reset.
- **Reaper:** a run in `running` without a database connection with
  `application_name = worker:<worker_id>` becomes `error` after
  `LAMPLIGHTER_REAPER_GRACE_S`, with the reason "worker lost" and a notification. For
  this every connection of a role has `application_name = <role>:<id>`.
- **Worker lock connection:** the `cancel_callback` checks the overlap lock every 5s.
  After a broken connection the worker takes it again. If that fails because another run
  holds it, the run becomes `error` with the reason "overlap lock lost".
- **Proxy:** uvicorn with `proxy_headers` and `forwarded_allow_ips =
  LAMPLIGHTER_TRUSTED_PROXIES`. That way audit and lockout see the real client IP, and a
  forged `X-Forwarded-For` from an unknown client is ignored.
- **`/metrics`:** with `LAMPLIGHTER_METRICS_TOKEN`, `Authorization: Bearer <token>` is
  required.
- **Host keys:**
  - A `known_hosts` credential (`ssh/<name>`, key `known_hosts`) on a template enforces
    strict checking: `StrictHostKeyChecking=yes` with its own `UserKnownHostsFile`.
  - Without `known_hosts`, `LAMPLIGHTER_ANSIBLE_HOST_KEY_CHECKING` applies. If that is
    on, the run is refused right away.
  - Every run gets its own SSH `ControlPath` (`ANSIBLE_SSH_CONTROL_PATH_DIR` in the
    private data dir). Otherwise a run could reuse another run's master connection via
    `ControlPersist`, and so bypass the host key check. The test for a wrong host key
    showed this.
- **Static files:** URLs get a content version (`?v=<hash>`), so browsers don't keep
  loading from their cache after an update.

**Acceptance criteria**
- A fresh LXC/VM comes up fully with one playbook run of the role, with a green
  healthcheck.
- An image update via `-e image_tag=<sha>` does not interrupt a running run.
- Retention and backup run as scheduled tasks and are tested with a restore.

---

## Ansible collections

**Scope:** collections for playbooks, in two ways.

- **Standard set in the image:** `collections/requirements.yml` in this repo (pinned:
  `ansible.posix`, `community.general`), installed at build time into
  `/usr/share/ansible/collections`. Works offline.
- **Per project:** a `collections/requirements.yml` in the project's repo (AWX
  convention) is installed by the worker with `ansible-galaxy` before the run
  (`app/worker/galaxy.py`). Cache in the shared volume `/var/cache/lamplighter/collections`,
  one directory per hash of the files under `collections/` (without vendored
  `ansible_collections/`) plus the ansible-core version. flock per key, install into a
  temp dir and rename, timeout `LAMPLIGHTER_COLLECTIONS_INSTALL_TIMEOUT_S` (600s),
  unused entries removed at worker start after `LAMPLIGHTER_COLLECTIONS_CACHE_DAYS` (30).
  `ANSIBLE_COLLECTIONS_PATH` = project cache, then ansible-core's defaults. Errors end the
  run as `error`, masked like git errors.
- **Setup notes in the run log:** lamplighter's own messages are events with `seq 0`
  (`lamplighter_note`); ansible-runner's start at 1. The events API and the stream
  therefore default to "after -1".
- **Deploy:** the role adds the `collections` volume and proxy variables
  (`lamplighter_http_proxy`, `lamplighter_https_proxy`, `lamplighter_no_proxy`) for the
  workers.
- **Tests:** a self-built test collection (`tests/fixtures/collection-src`) in a fixture
  repo as a local tarball, so the integration tests need no Galaxy access.

## Logout at Keycloak and the dashboard

- **RP-initiated logout:** "Sign out" of an OIDC user also ends the session at Keycloak,
  via the `end_session_endpoint` from discovery with `id_token_hint` and
  `post_logout_redirect_uri` = `<public_url>/ui/login?signed_out=1`. The id_token is kept
  in its own cookie (`lamplighter_idt`: HttpOnly, Secure, SameSite=Lax, path
  `/ui/logout`), not in the database. Without that cookie the logout goes via
  `client_id` and Keycloak asks for confirmation; without an `end_session_endpoint` only
  the local session ends. Keycloak's "Valid post logout redirect URIs" must contain
  `<public_url>/ui/login*`.
- **Dashboard** (`/ui/dashboard`, the start page; `app/services/dashboard.py`): tiles for
  the last 24 hours (runs, successful, failed, success rate, running, queued), runs per
  hour as a stacked server-rendered SVG, failing schedules (last finished run not
  successful), the next 5 scheduled runs, the last 10 failures and the maintenance status
  (backup and retention; overdue after 26 hours). Refreshes every 15s via htmx.

## Relaunch

- A finished run can be relaunched (UI button on the run page and
  `POST /api/v1/runs/{id}/relaunch`, `launch` rights): a new manual run of the same
  template with the original's effective extra vars and limit, the template's current
  settings and the latest commit. "Relaunch failed hosts" (`failed_hosts_only`) limits it
  to the hosts that failed or were unreachable (from the stats). `runs.relaunch_of`
  (nullable, `ON DELETE SET NULL`) links the new run to the original; the audit entry
  `run.relaunch` records `relaunched_from`. A run that is still queued or running gives
  409.

## Phase 6 — Kubernetes (Helm)

**Scope:** a Helm chart as an alternative to Docker Compose: deployments for api,
scheduler and worker (with `terminationGracePeriodSeconds` for running runs), the migrate
job as a hook, a PodDisruptionBudget, secrets via the cluster's OpenBao integration, and
Postgres external or via an operator.

---

## Open items

- **Collections, later:** `roles/requirements.yml` (Galaxy roles), a private Galaxy or
  Automation Hub with a token from OpenBao, and signature verification of collections.
- **SSE scales per thread:** every open stream occupies a thread from the thread pool and
  polls the database. That is fine for dev and small scale. With more viewers:
  `LISTEN/NOTIFY` on new events or an async generator (phase 5).
- **Role changes in Keycloak** only take effect at the next login: the session keeps a
  snapshot, which stays valid for at most 24 hours. Bearer tokens follow immediately,
  because they only live for 5 minutes.
- **`/healthz` and `/readyz` are open** (meant for load balancers and healthchecks).
  Since 5a `/metrics` has an optional scrape token; shielding it via the proxy comes in
  5b.
- **Lockout per username:** prevents brute force on a single account. An attacker can
  use it to lock an account temporarily, though. A rate limit per IP is added in phase 5,
  via the proxy.
- **UI on narrow screens:** since the sidebar layout the menu slides in and wide tables
  scroll inside the content area, but the tables themselves are still made for desktop
  (no stacked card view on a phone).
- **Remote processes on cancel/timeout:** ansible-runner stops the local ansible process;
  a running command on the target (e.g. `sleep`) keeps running there.
- **AppRole secret ids** are stored as an env file on the host (0600, root). Better:
  response wrapping or a short-lived, CIDR-bound secret id per deploy, minted by the
  Ansible role. That needs an OpenBao token with rights on the AppRoles during the
  deploy, which is a separate security decision.
- **The role does not install Docker** and does not manage nginx or certificates; both
  are the host's responsibility.
- **Local test of the role:** only ansible-lint and a syntax check run locally; the
  full deploy test needs a throwaway Linux host with Docker and systemd (CI). A local VM
  (Lima/UTM) would make it possible on a Mac.
- **Old images** are not pruned by the role; `docker image prune` now and then.
- **The dev OpenBao is in-memory:** if you restart only `openbao`, the secrets are gone
  until `openbao-init` runs again (`podman compose up -d`).
