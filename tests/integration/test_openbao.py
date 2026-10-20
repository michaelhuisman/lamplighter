"""Phase 4b: credentials from OpenBao, git via https with a token, webhooks from OpenBao."""

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import hvac
import pytest
from hvac import exceptions as hvac_exc
from sqlalchemy import text

from app.core.db import get_engine
from tests.integration.conftest import SECRETS_DIR, Env, fixture_head, post, unique

OPENBAO = os.environ.get("LAMPLIGHTER_IT_OPENBAO", "http://127.0.0.1:8200")
GIT_HTTP = os.environ.get("LAMPLIGHTER_IT_GIT_HTTP", "http://127.0.0.1:8081")
SINK = os.environ.get("LAMPLIGHTER_IT_WEBHOOK_SINK", "http://127.0.0.1:8080")
REPO_CACHE = Path("/var/cache/lamplighter/repos")
WEBHOOK_PATH = "webhooks/default"


@pytest.fixture(scope="module")
def bao() -> hvac.Client:
    """Root client (dev only) to write test secrets."""
    token = (SECRETS_DIR / "openbao" / "root-token").read_text().strip()
    return hvac.Client(url=OPENBAO, token=token)


def put(bao: hvac.Client, path: str, **values: str) -> None:
    bao.secrets.kv.v2.create_or_update_secret(path=path, secret=values, mount_point="secret")


def approle(name: str) -> hvac.Client:
    env = dict(
        line.split("=", 1)
        for line in (SECRETS_DIR / "openbao" / f"{name}.env").read_text().splitlines()
        if line
    )
    client = hvac.Client(url=OPENBAO)
    client.auth.approle.login(
        role_id=env["LAMPLIGHTER_OPENBAO_ROLE_ID"], secret_id=env["LAMPLIGHTER_OPENBAO_SECRET_ID"]
    )
    return client


def credential(env: Env, type_: str, path: str, key: str) -> dict[str, Any]:
    return post(
        env.api,
        "/credentials",
        {"name": unique(type_), "type": type_, "openbao_path": path, "openbao_key": key},
    )


def git_project(env: Env, credential_id: int | None) -> dict[str, Any]:
    return post(
        env.api,
        "/projects",
        {
            "name": unique("https"),
            "git_url": f"{GIT_HTTP}/repo.git",
            "branch": "main",
            "credential_id": credential_id,
        },
    )


def template_for(env: Env, project: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return post(
        env.api,
        "/templates",
        {
            "name": unique("tpl"),
            "project_id": project["id"],
            "playbook_path": "ping.yml",
            "inventory_id": env.inventory["id"],
            "machine_credential_id": env.credential["id"],
            **extra,
        },
    )


def db_contains(needle: str) -> list[str]:
    """Tables in which `needle` occurs somewhere in a row."""
    hits = []
    with get_engine().connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        for table in tables:
            found = conn.execute(
                text(f'SELECT 1 FROM "{table}" t WHERE to_jsonb(t)::text LIKE :n LIMIT 1'),  # noqa: S608
                {"n": f"%{needle}%"},
            ).first()
            if found:
                hits.append(table)
    return hits


# --- policies ---------------------------------------------------------------------


def test_approles_have_least_privilege() -> None:
    worker, scheduler = approle("worker"), approle("scheduler")
    assert worker.secrets.kv.v2.read_secret_version(
        path="ssh/ssh-target", mount_point="secret", raise_on_deleted_version=True
    )
    with pytest.raises(hvac_exc.Forbidden):
        worker.secrets.kv.v2.read_secret_version(path=WEBHOOK_PATH, mount_point="secret")
    assert scheduler.secrets.kv.v2.read_secret_version(
        path=WEBHOOK_PATH, mount_point="secret", raise_on_deleted_version=True
    )
    with pytest.raises(hvac_exc.Forbidden):
        scheduler.secrets.kv.v2.read_secret_version(path="ssh/ssh-target", mount_point="secret")


def test_worker_cannot_read_playbook_secrets_itself() -> None:
    """Playbook secrets (mount ansible) are only readable with a run token."""
    with pytest.raises(hvac_exc.Forbidden):
        approle("worker").secrets.kv.v2.read_secret_version(
            path="dev/lamplighter", mount_point="ansible", raise_on_deleted_version=True
        )


# --- run token (community.hashi_vault) -----------------------------------------------


def test_run_token_for_hashi_vault_lookups(env: Env, bao: hvac.Client) -> None:
    template = env.template("hashi-vault.yml", timeout_s=120)
    run = env.wait(env.launch(template)["id"])
    assert run["status"] == "successful", run
    stdout = "\n".join(e["stdout"] or "" for e in env.events(run["id"]))

    value = (SECRETS_DIR / "ansible" / "value").read_text().strip()
    assert f"secret_sha256={hashlib.sha256(value.encode()).hexdigest()}" in stdout

    props = re.search(
        r"accessor=(\S+) policies=(\S+) ttl=(\d+) renewable=(\w+) orphan=(\w+) run_id=(\d+)",
        stdout,
    )
    assert props, stdout
    accessor, policies, ttl, renewable, orphan, run_id = props.groups()
    assert policies == "ansible-read,default"
    assert 0 < int(ttl) <= 120  # TTL = the template's timeout
    assert (renewable, orphan, int(run_id)) == ("False", "True", run["id"])

    # The token value itself is masked in the run log.
    assert re.search(r"token=\*+", stdout), stdout
    # Revoked after the run (revoke-self).
    with pytest.raises((hvac_exc.InvalidRequest, hvac_exc.Forbidden)):
        bao.auth.token.lookup_accessor(accessor)
    assert db_contains(value) == []


# --- credentials ------------------------------------------------------------------


def test_missing_secret_gives_clear_error(env: Env) -> None:
    cred = credential(env, "ssh_key", f"ssh/{uuid.uuid4().hex}", "id_ed25519")
    run = env.wait(env.launch(env.template("ping.yml", machine_credential_id=cred["id"]))["id"])
    assert run["status"] == "error"
    assert f"credential {cred['id']}" in run["status_reason"]
    assert "not found" in run["status_reason"]


def test_known_secrets_never_stored(env: Env, bao: hvac.Client) -> None:
    marker = f"vault-marker-{uuid.uuid4().hex}"
    path = f"vault/{unique('it')}"
    put(bao, path, password=marker)
    vault = credential(env, "vault_password", path, "password")
    template = env.template("nolog.yml", vault_credential_id=vault["id"])
    run = env.wait(env.launch(template, extra_vars={"secret_value": unique("x")})["id"])
    assert run["status"] == "successful", run

    git_token = (SECRETS_DIR / "git" / "token").read_text().strip()
    ssh_key = (SECRETS_DIR / "ssh-target" / "id_ed25519").read_text()
    key_line = next(line for line in ssh_key.splitlines() if "-----" not in line)
    for needle in (marker, git_token, key_line):
        assert db_contains(needle) == [], f"secret found in database: {needle[:8]}…"


# --- git via https with a token -------------------------------------------------------


def test_git_https_with_token(env: Env) -> None:
    cred = credential(env, "git_token", "git/fixtures", "token")
    project = git_project(env, cred["id"])
    run = env.wait(env.launch(template_for(env, project))["id"])
    assert run["status"] == "successful", run
    assert run["commit_sha"] == fixture_head()

    # The token is nowhere in the bare cache repo.
    token = (SECRETS_DIR / "git" / "token").read_text().strip()
    repo = REPO_CACHE / f"{project['id']}.git"
    assert repo.is_dir()
    for file in repo.rglob("*"):
        if file.is_file() and file.stat().st_size < 1_000_000:
            assert token.encode() not in file.read_bytes(), f"token in {file}"
    assert "credential" not in (repo / "config").read_text()


def test_git_https_wrong_token(env: Env, bao: hvac.Client) -> None:
    wrong = f"wrong-{uuid.uuid4().hex}"
    path = f"git/{unique('bad')}"
    put(bao, path, token=wrong, username="scheduler")
    project = git_project(env, credential(env, "git_token", path, "token")["id"])
    run = env.wait(env.launch(template_for(env, project))["id"])
    assert run["status"] == "error"
    assert run["status_reason"].startswith("git clone failed")
    assert wrong not in run["status_reason"]


def test_git_https_without_credential(env: Env) -> None:
    project = git_project(env, None)
    run = env.wait(env.launch(template_for(env, project))["id"])
    assert run["status"] == "error"
    assert "git clone failed" in run["status_reason"]


def test_project_credential_must_be_git_token(env: Env) -> None:
    project = git_project(env, env.credential["id"])  # an ssh_key
    run = env.wait(env.launch(template_for(env, project))["id"])
    assert run["status"] == "error"
    assert "not of type git_token" in run["status_reason"]


# --- webhooks from OpenBao ----------------------------------------------------------


@pytest.fixture
def restore_webhooks(bao: hvac.Client) -> Iterator[None]:
    current = bao.secrets.kv.v2.read_secret_version(
        path=WEBHOOK_PATH, mount_point="secret", raise_on_deleted_version=True
    )["data"]["data"]
    yield
    put(bao, WEBHOOK_PATH, **current)


def deliveries(run_id: int) -> list[dict[str, Any]]:
    received = httpx.get(f"{SINK}/received", timeout=5).json()["received"]
    return [r for r in received if json.loads(r["raw"])["run_id"] == run_id]


def wait_for(fn: Any, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := fn():
            return result
        time.sleep(1)
    pytest.fail(f"condition not met within {timeout}s")


@pytest.mark.usefixtures("restore_webhooks")
def test_webhook_rotation_without_restart(env: Env, bao: hvac.Client) -> None:
    hmac_secret = (SECRETS_DIR / "webhook" / "hmac").read_text().strip()
    put(
        bao,
        WEBHOOK_PATH,
        urls=json.dumps([f"{SINK}/hook?v=rotated"]),
        hmac_secret=hmac_secret,
    )
    time.sleep(6)  # cache in dev: 5s
    run = env.wait(env.launch(env.template("fail.yml"))["id"])
    assert run["status"] == "failed"
    [delivery] = wait_for(lambda: deliveries(run["id"]), timeout=30)
    assert delivery["path"] == "/hook?v=rotated"
