"""Integration tests against compose.dev.yml. Run in the `dev` container:

podman compose -f compose.dev.yml run --rm dev pytest tests/integration
"""

import os
import re
import secrets
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.db import get_engine, get_sessionmaker
from app.services import api_tokens, users

API_URL = os.environ.get("LAMPLIGHTER_IT_API_URL", "http://127.0.0.1:8000")

# Everything the tests create, emptied at the start of every test session so the dev
# environment does not keep filling up. Users are handled separately (see below).
_TEST_TABLES = (
    "runs, run_events, notifications, schedules, templates, inventories, projects,"
    " credentials, categories, audit_log, run_stats_archive, maintenance_status"
)
# Local users the tests create: it-admin, it-viewer, it-tok-1a2b3c4d, dash-1a2b3c4d, ...
# Other accounts (your own admin, Keycloak users) are kept.
_TEST_USER = r"^(it-.*|[a-z]+(-[a-z]+)*-[0-9a-f]{8})$"
FIXTURE_REPO = Path("/fixtures/repo.git")
RUNTIME_DIR = Path("/run/lamplighter")
SECRETS_DIR = Path("/secrets")
TERMINAL = {"successful", "failed", "error", "timeout", "canceled", "skipped"}
INLINE_INVENTORY = "ssh-target ansible_user=ansible ansible_python_interpreter=/usr/bin/python3\n"

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class LocalUser:
    username: str
    password: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def ensure_local_user(username: str, roles: list[str]) -> LocalUser:
    """Create (or reset) a local test user with a fresh password and API token."""
    password = secrets.token_urlsafe(18)
    with get_sessionmaker()() as s:
        user = users.get_local(s, username)
        if user is None:
            user = users.create_local(s, username, password, roles)
        else:
            users.set_password(s, user.id, password)
            user = users.update_local(s, user.id, roles=roles, disabled=False)
        token = api_tokens.create(s, user, "integration tests", expires_days=1)
    return LocalUser(username, password, token.raw)


@pytest.fixture(scope="session", autouse=True)
def clean_dev_database() -> None:
    """Start every test session with an empty dev environment.

    Only inside the dev container (LAMPLIGHTER_IT_API_URL is set there), never against
    another database. Set LAMPLIGHTER_IT_KEEP_DATA=1 to keep the data, e.g. while
    debugging a single test.
    """
    if "LAMPLIGHTER_IT_API_URL" not in os.environ or os.environ.get("LAMPLIGHTER_IT_KEEP_DATA"):
        return
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {_TEST_TABLES} RESTART IDENTITY CASCADE"))
        # Sessions and API tokens of these users go with them (ON DELETE CASCADE).
        conn.execute(
            text("DELETE FROM users WHERE source = 'local' AND username ~ :pattern"),
            {"pattern": _TEST_USER},
        )


@pytest.fixture(scope="session")
def admin() -> LocalUser:
    return ensure_local_user("it-admin", ["admin"])


@pytest.fixture(scope="session")
def api(admin: LocalUser) -> Iterator[httpx.Client]:
    try:
        httpx.get(f"{API_URL}/readyz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"API not reachable at {API_URL}: {exc}")
    with httpx.Client(base_url=f"{API_URL}/api/v1", timeout=10, headers=admin.headers) as client:
        yield client


def login_ui(username: str, password: str) -> httpx.Client:
    """UI client with session cookie; sends the CSRF token as a header."""
    client = httpx.Client(base_url=f"{API_URL}/ui", timeout=10, follow_redirects=False)
    resp = client.post(
        "/login", data={"username": username, "password": password, "next": "/ui/runs"}
    )
    assert resp.status_code == 303, resp.text
    page = client.get("/runs").text
    match = re.search(r'"X-CSRF-Token": "([^"]+)"', page)
    assert match, "no CSRF token on page"
    client.headers["X-CSRF-Token"] = match.group(1)
    return client


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def post(api: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = api.post(path, json=body)
    assert resp.status_code in (200, 201, 202), resp.text
    result: dict[str, Any] = resp.json()
    return result


def fixture_head() -> str:
    return subprocess.run(
        ["git", "-C", str(FIXTURE_REPO), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class Env:
    """Base configuration: credential, project and inline inventory against ssh-target."""

    def __init__(self, api: httpx.Client) -> None:
        self.api = api
        self.credential = post(
            api,
            "/credentials",
            {
                "name": unique("key"),
                "type": "ssh_key",
                "openbao_path": "ssh/ssh-target",
                "openbao_key": "id_ed25519",
            },
        )
        self.project = post(
            api,
            "/projects",
            {"name": unique("proj"), "git_url": f"file://{FIXTURE_REPO}", "branch": "main"},
        )
        self.inventory = post(
            api,
            "/inventories",
            {"name": unique("inv"), "source_type": "inline", "content": INLINE_INVENTORY},
        )

    def template(self, playbook: str, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": unique(playbook.removesuffix(".yml")),
            "project_id": self.project["id"],
            "playbook_path": playbook,
            "inventory_id": self.inventory["id"],
            "machine_credential_id": self.credential["id"],
        }
        body.update(overrides)
        return post(self.api, "/templates", body)

    def launch(self, template: dict[str, Any], **body: Any) -> dict[str, Any]:
        return post(self.api, f"/templates/{template['id']}/launch", body)

    def get_run(self, run_id: int) -> dict[str, Any]:
        resp = self.api.get(f"/runs/{run_id}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    def wait(
        self, run_id: int, statuses: set[str] = TERMINAL, timeout: float = 90
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] in statuses:
                return run
            if time.monotonic() > deadline:
                pytest.fail(f"run {run_id} stuck in {run['status']}")
            time.sleep(0.5)

    def events(self, run_id: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after = -1  # seq 0: lamplighter's own setup notes
        while True:
            resp = self.api.get(f"/runs/{run_id}/events", params={"after_seq": after})
            resp.raise_for_status()
            page = resp.json()
            items.extend(page["items"])
            if page["next_after_seq"] is None:
                return items
            after = page["next_after_seq"]


@pytest.fixture(scope="session")
def env(api: httpx.Client) -> Env:
    return Env(api)
