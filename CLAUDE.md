# CLAUDE.md — lamplighter

Application that runs Ansible playbooks on a schedule and on demand. One codebase and one
image with three roles: `api`, `scheduler` and `worker` (plus a one-shot `migrate`).
Names: compose project and image `lamplighter`, env prefix `LAMPLIGHTER_`, paths
`/run/lamplighter` and `/var/cache/lamplighter`. The full design and the phasing are in
`docs/plan.md`. Read that file before starting on a phase.

## Stack (pinned, do not deviate without discussion)

- Python 3.12
- FastAPI + uvicorn (API)
- Jinja2 + htmx for the UI (server-rendered; htmx vendored, no Node toolchain)
- prometheus-client (metrics), httpx (webhooks)
- SQLAlchemy 2.x, **synchronous** sessions, `psycopg` 3 as driver
- Alembic for all schema changes
- APScheduler 3.x with `SQLAlchemyJobStore` on Postgres
- ansible-core (pinned version in `requirements.txt`) + ansible-runner
- pydantic v2 + pydantic-settings for config
- PyJWT (OIDC/JWKS), argon2-cffi (passwords of local users)
- hvac for OpenBao (from phase 4)
- pytest, ruff (lint + format), mypy (strict on `app/`)
- Postgres 18
- Docker Compose for dev and production

## Structure

```
app/
  __main__.py      # python -m app {api|scheduler|worker|migrate}
  api/             # FastAPI routers, schemas (pydantic)
  ui/              # server-rendered UI: Jinja2 templates, htmx (vendored) and statics
  scheduler/       # APScheduler setup, leader election, schedule -> run enqueue
  worker/          # queue consumer, ansible-runner wrapper, git checkout
  models/          # SQLAlchemy models
  services/        # business logic, separate from API and worker
  core/            # config, db session, logging, auth, oidc and openbao client
migrations/        # Alembic
tests/
  unit/
  integration/     # runs against compose.dev.yml
deploy/
  roles/lamplighter/ # Ansible role: production compose (template), backup timer, rolling update
  site.yml, restore.yml
  tests/           # deploy-test (CI only: writes to /opt and systemd on the runner)
docs/plan.md, docs/deploy/  # design; nginx example, backup and restore
collections/requirements.yml  # standard set of Ansible collections in the image
compose.dev.yml    # dev: postgres, ssh-target, keycloak, openbao, git-http, webhook-sink
```

## Commands

Containers run with Podman (`podman compose`), not with Docker Desktop. Tooling runs in
the `dev` container (Python 3.12, source code mounted); no local 3.12 is needed.

```bash
# dev environment (once: scripts/dev-keys.sh)
podman compose -f compose.dev.yml up -d --build --scale worker=2 --scale scheduler=2
# after a code change a restart is enough (app/ and migrations/ are mounted)
podman compose -f compose.dev.yml restart api worker scheduler
# first local admin (password via prompt)
podman compose -f compose.dev.yml run --rm dev python -m app create-user admin --role admin

# quality, in the dev container
DEV="podman compose -f compose.dev.yml run --rm dev"
$DEV sh -c 'ruff check . && ruff format --check .'
$DEV mypy app
$DEV pytest tests/unit
$DEV pytest tests/integration     # requires a running compose.dev.yml; empties the dev
                                  # data first (keeps your own users; LAMPLIGHTER_IT_KEEP_DATA=1 skips)
$DEV pytest tests/integration -m "not slow"   # without the tests that wait minutes for cron
scripts/it-failover.sh            # on the host: kill the scheduler leader, check takeover
scripts/it-secret-scan.sh         # on the host: no dev secrets in the container logs
# the scripts pick the runtime via $CONTAINER (podman if available, otherwise docker)

# migrations
$DEV alembic revision --autogenerate -m "<description>"
podman compose -f compose.dev.yml run --rm migrate

# Ansible role: lint locally; the full deploy test only runs in CI (it needs Docker and
# systemd on a throwaway host)
$DEV sh -c 'pip install -q -r deploy/requirements.txt && cd deploy &&
  ansible-galaxy collection install -r requirements.yml && ansible-lint'
```

## CI

GitHub Actions (`.github/workflows/ci.yml`): lint, ansible-lint, unit, integration
(compose.dev.yml with Docker, including the slow tests, failover and secret scan), the
deploy test of the Ansible role, a Trivy scan of the runtime image (fails on fixable
HIGH/CRITICAL; exceptions in `.trivyignore` with a reason) and the image to GHCR
(`ghcr.io/<owner>/lamplighter`, only `main` and `v*` tags). Pin actions to a commit SHA and
container images used in CI to a digest.

## Conventions

- Full type hints. No `Any` without a reason.
- Config only via `app/core/config.py` (pydantic-settings, env prefix `LAMPLIGHTER_`).
  Never read `os.environ` directly.
- Business logic in `app/services/`. Routers and the worker loop are thin.
- Every schema change gets an Alembic migration. Never `metadata.create_all()` outside
  tests. Migrations must stay compatible with the previous version: during an update old
  workers keep running until their run is done.
- Always store time timezone-aware in UTC. A schedule has its own time zone field.
- Logging via stdlib `logging` in JSON format to stdout. No `print`.
- All text in the repo is English: UI, code comments, docstrings, CLI and script output,
  docs. Commit messages and pull requests are English too.
- The worker runs ansible-runner without process isolation (`process_isolation=False`).
  Ansible runs directly in the worker container.
- The ansible-runner private data dir lives under `LAMPLIGHTER_RUNTIME_DIR` (tmpfs) and is
  removed after every run, also on an exception.
- Queue claims use `SELECT ... FOR UPDATE SKIP LOCKED`. Overlap per template and leader
  election use Postgres advisory locks. No extra infrastructure (Redis etc.).

## Safety rules while building

- Never talk to real hosts, real inventories or a real OpenBao. Integration tests run
  exclusively against the `ssh-target` container from `compose.dev.yml`.
- Never commit secrets. Dev keys are generated by `scripts/dev-keys.sh` and are listed in
  `.gitignore`.
- Secrets never go into the database, logs or run_events. Credentials are references.
  ansible-runner events are filtered for `no_log` and known secret fields before
  storage.

## Way of working

- Work per phase from `docs/plan.md`. Make a plan first and wait for approval.
- A phase is done when all acceptance criteria of that phase are met and ruff, mypy and
  pytest (unit + integration) are green.
- Keep changes within the scope of the phase. Flag things for later phases in
  `docs/plan.md` under "Open items" instead of building them right away.
