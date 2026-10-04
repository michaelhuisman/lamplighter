"""Phase 3: SSE stream, metrics, webhooks and the UI."""

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import Notification, Run
from tests.integration.conftest import (
    API_URL,
    SECRETS_DIR,
    Env,
    LocalUser,
    ensure_local_user,
    login_ui,
    post,
    unique,
)

SINK = os.environ.get("LAMPLIGHTER_IT_WEBHOOK_SINK", "http://127.0.0.1:8080")
WEBHOOK_SECRET = (SECRETS_DIR / "webhook" / "hmac").read_text().strip()  # in OpenBao
METRICS_AUTH = {"Authorization": f"Bearer {(SECRETS_DIR / 'metrics-token').read_text().strip()}"}


# --- SSE -----------------------------------------------------------------------


def read_stream(
    run_id: int, headers: dict[str, str], timeout: float = 60
) -> list[tuple[str, dict[str, Any], str | None]]:
    events: list[tuple[str, dict[str, Any], str | None]] = []
    current: dict[str, str] = {}
    with httpx.stream(
        "GET", f"{API_URL}/api/v1/runs/{run_id}/stream", headers=headers, timeout=timeout
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line == "":
                if "event" in current:
                    events.append(
                        (current["event"], json.loads(current["data"]), current.get("id"))
                    )
                    if current["event"] == "end":
                        return events
                current = {}
            elif not line.startswith(":"):
                key, _, value = line.partition(": ")
                current[key] = value
    return events


def test_stream_follows_running_run(env: Env) -> None:
    template = env.template("steps.yml", extra_vars={"step_s": 1})
    run_id = env.launch(template)["id"]

    events = read_stream(run_id, dict(env.api.headers))

    kinds = [k for k, _, _ in events]
    assert kinds[-1] == "end"
    assert events[-1][1]["status"] == "successful"
    statuses = [d["status"] for k, d, _ in events if k == "status"]
    assert statuses[-1] == "successful"
    assert "running" in statuses or statuses[0] in ("queued", "running")

    run_events = [(d, i) for k, d, i in events if k == "run_event"]
    seqs = [d["seq"] for d, _ in run_events]
    assert seqs == sorted(seqs)
    assert [str(s) for s in seqs] == [i for _, i in run_events]
    stdout = "".join(d["stdout"] for d, _ in run_events)
    assert "PLAY RECAP" in stdout
    assert "Identity added" not in stdout
    # The same events as via the regular events API.
    assert seqs == [e["seq"] for e in env.events(run_id)]


def test_stream_resumes_after_last_event_id(env: Env) -> None:
    run_id = env.wait(env.launch(env.template("ping.yml"))["id"])["id"]
    all_seqs = [e["seq"] for e in env.events(run_id)]
    cut = all_seqs[len(all_seqs) // 2]
    events = read_stream(run_id, {**env.api.headers, "Last-Event-ID": str(cut)})
    seqs = [d["seq"] for k, d, _ in events if k == "run_event"]
    assert seqs == [s for s in all_seqs if s > cut]


def test_stream_unknown_run(env: Env) -> None:
    resp = httpx.get(f"{API_URL}/api/v1/runs/999999/stream", headers=env.api.headers)
    assert resp.status_code == 404


def test_stream_requires_auth(env: Env) -> None:
    run_id = env.wait(env.launch(env.template("ping.yml"))["id"])["id"]
    assert httpx.get(f"{API_URL}/api/v1/runs/{run_id}/stream").status_code == 401


# --- metrics ---------------------------------------------------------------------


def metric(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_metrics_update_after_runs(env: Env) -> None:
    template = env.template("ping.yml")
    name = template["name"]
    before = httpx.get(f"{API_URL}/metrics", headers=METRICS_AUTH).text
    assert metric(before, "lamplighter_runs_total", template=name) is None

    env.wait(env.launch(template)["id"])
    env.wait(env.launch(template)["id"])

    after = httpx.get(f"{API_URL}/metrics", headers=METRICS_AUTH)
    assert after.status_code == 200
    assert after.headers["content-type"].startswith("text/plain")
    text = after.text
    assert metric(text, "lamplighter_runs_total", template=name, status="successful") == 2
    assert metric(text, "lamplighter_run_duration_seconds_count", template=name) == 2
    assert metric(text, "lamplighter_queue_depth") is not None


# --- webhooks --------------------------------------------------------------------


def sink(path: str, method: str = "GET") -> dict[str, Any]:
    resp = httpx.request(method, f"{SINK}{path}", timeout=5)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def deliveries_for(run_id: int) -> list[dict[str, Any]]:
    out = []
    for item in sink("/received")["received"]:
        body = json.loads(item["raw"])
        if body["run_id"] == run_id:
            out.append({"headers": item["headers"], "body": body, "raw": item["raw"]})
    return out


def wait_for(fn: Any, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(1)
    pytest.fail(f"condition not met within {timeout}s")


def no_pending_notifications() -> bool:
    with get_sessionmaker()() as s:
        return s.scalar(select(Notification.id).where(Notification.status == "pending")) is None


def notification_for(run_id: int) -> Notification | None:
    with get_sessionmaker()() as s:
        return s.scalar(select(Notification).where(Notification.run_id == run_id))


def test_webhook_on_failed_run(env: Env) -> None:
    run = env.wait(env.launch(env.template("fail.yml"))["id"])
    assert run["status"] == "failed"

    [delivery] = wait_for(lambda: deliveries_for(run["id"]), timeout=30)
    body = delivery["body"]
    assert body["event"] == "run.failed"
    assert body["status"] == "failed"
    assert body["template"]["name"].startswith("fail-")
    assert body["url"].endswith(f"/ui/runs/{run['id']}")

    headers = {k.lower(): v for k, v in delivery["headers"].items()}
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), delivery["raw"].encode(), hashlib.sha256
    ).hexdigest()
    assert headers["x-lamplighter-signature"] == f"sha256={expected}"

    note = notification_for(run["id"])
    assert note is not None
    assert note.status == "sent"
    assert note.attempts == 1
    # No URL in the database, only the fingerprint.
    assert "webhook-sink" not in note.target


def test_webhook_retries_with_backoff(env: Env) -> None:
    wait_for(no_pending_notifications, timeout=60)
    sink("/fail?n=2", method="POST")
    run = env.wait(env.launch(env.template("fail.yml"))["id"])

    [delivery] = wait_for(lambda: deliveries_for(run["id"]), timeout=60)
    assert delivery["body"]["status"] == "failed"
    note = notification_for(run["id"])
    assert note is not None
    assert note.status == "sent"
    assert note.attempts == 3


def test_no_webhook_for_successful_run(env: Env) -> None:
    run = env.wait(env.launch(env.template("ping.yml"))["id"])
    assert run["status"] == "successful"
    assert notification_for(run["id"]) is None


# --- UI ----------------------------------------------------------------------------


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def test_ui_pages_render(ui: httpx.Client, env: Env) -> None:
    template = env.template("ping.yml")
    run = env.wait(env.launch(template)["id"])
    for path in (
        "/dashboard",
        "/runs",
        "/templates",
        "/schedules",
        "/templates/new",
        "/schedules/new",
        f"/runs/{run['id']}",
        f"/templates/{template['id']}/edit",
        f"/templates/{template['id']}/launch",
    ):
        resp = ui.get(path)
        assert resp.status_code == 200, path
        assert "<!doctype html>" in resp.text.lower(), path
    assert template["name"] in ui.get("/templates").text
    assert httpx.get(f"{API_URL}/", follow_redirects=False).headers["location"] == "/ui/dashboard"


def test_ui_runs_partial_for_htmx(ui: httpx.Client, env: Env) -> None:
    template = env.template("ping.yml")
    run = env.wait(env.launch(template)["id"])
    resp = ui.get("/runs", params={"template_id": template["id"]}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert f'href="/ui/runs/{run["id"]}"' in resp.text


def test_ui_template_create_validation_and_launch(ui: httpx.Client, env: Env) -> None:
    name = unique("ui-tpl")
    form = {
        "name": name,
        "project_id": str(env.project["id"]),
        "playbook_path": "ping.yml",
        "inventory_id": str(env.inventory["id"]),
        "machine_credential_id": str(env.credential["id"]),
        "vault_credential_id": "",
        "extra_vars": "{not json",
        "verbosity": "0",
        "timeout_s": "",
    }
    bad = ui.post("/templates", data=form)
    assert bad.status_code == 422
    assert "invalid JSON" in bad.text
    assert name in bad.text  # ingevulde waarden blijven staan

    form["extra_vars"] = '{"greeting": "<script>alert(1)</script>"}'
    ok = ui.post("/templates", data=form)
    assert ok.status_code == 303
    listing = ui.get("/templates").text
    assert name in listing

    template = next(t for t in env.api.get("/templates").json() if t["name"] == name)
    launch_page = ui.get(f"/templates/{template['id']}/launch").text
    assert "<script>alert(1)</script>" not in launch_page  # ge-escaped
    launched = ui.post(f"/templates/{template['id']}/launch", data={"extra_vars": "", "limit": ""})
    assert launched.status_code == 303
    match = re.fullmatch(r"/ui/runs/(\d+)", launched.headers["location"])
    assert match
    run = env.wait(int(match.group(1)))
    assert run["status"] == "successful"
    assert run["extra_vars"]["greeting"] == "<script>alert(1)</script>"


def test_ui_schedule_create_toggle_delete(ui: httpx.Client, env: Env) -> None:
    template = env.template("ping.yml")
    bad = ui.post(
        "/schedules",
        data={"template_id": str(template["id"]), "cron": "0 9 1 * 1", "timezone": "UTC"},
    )
    assert bad.status_code == 422
    assert "day of week" in bad.text

    ok = ui.post(
        "/schedules",
        data={
            "template_id": str(template["id"]),
            "cron": "0 3 * * *",
            "timezone": "Europe/Amsterdam",
            "overlap_policy": "skip",
            "misfire_grace_s": "60",
            "extra_vars_override": "",
        },
    )
    assert ok.status_code == 303
    sched = next(s for s in env.api.get("/schedules").json() if s["template_id"] == template["id"])
    assert sched["enabled"] is False  # checkbox not ticked

    row = ui.post(f"/schedules/{sched['id']}/toggle")
    assert row.status_code == 200
    assert "<tr>" in row.text
    assert env.api.get(f"/schedules/{sched['id']}").json()["enabled"] is True

    assert ui.post(f"/schedules/{sched['id']}/delete").status_code == 200
    assert env.api.get(f"/schedules/{sched['id']}").status_code == 404


def test_ui_cancel_running_run(ui: httpx.Client, env: Env) -> None:
    run_id = env.launch(env.template("sleep.yml", extra_vars={"sleep_s": 60}))["id"]
    env.wait(run_id, {"running"})
    resp = ui.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert "cancel requested" in resp.text
    assert env.wait(run_id, timeout=30)["status"] == "canceled"


def test_ui_delete_template_with_active_run_shows_error(ui: httpx.Client, env: Env) -> None:
    # Finished runs no longer block deleting (they stay in the history); active runs do.
    template = env.template("sleep.yml", extra_vars={"sleep_s": 30})
    run = env.launch(template)
    env.wait(run["id"], {"running"})
    resp = ui.post(f"/templates/{template['id']}/delete")
    assert resp.status_code == 409
    assert "queued or running" in resp.text
    env.api.post(f"/runs/{run['id']}/cancel")
    env.wait(run["id"])


# --- dashboard ------------------------------------------------------------------------


def test_dashboard_shows_failures_and_failing_schedules(env: Env) -> None:
    template = env.template("fail.yml")
    schedule = post(
        env.api,
        "/schedules",
        {"template_id": template["id"], "cron": "0 0 1 1 *", "timezone": "UTC"},
    )
    with get_sessionmaker()() as s, s.begin():
        s.add(
            Run(
                template_id=template["id"],
                schedule_id=schedule["id"],
                scheduled_for=datetime.now(UTC) - timedelta(minutes=5),
                triggered_by="schedule",
                status="failed",
                rc=2,
                finished_at=datetime.now(UTC),
            )
        )
    me = ensure_local_user(unique("dash"), ["viewer"])
    ui = login_ui(me.username, me.password)
    page = ui.get("/dashboard")
    assert page.status_code == 200
    text = page.text
    assert "Runs per hour" in text
    failing = text.split("Failing schedules", 1)[1].split("Upcoming runs", 1)[0]
    assert template["name"] in failing
    # Which schedules are "upcoming" depends on all schedules in the database; the
    # selection itself is unit-tested (test_dashboard.py).
    assert "Upcoming runs" in text
    assert "Maintenance" in text
    # The htmx refresh returns only the panels.
    partial = ui.get("/dashboard", headers={"HX-Request": "true"}).text
    assert 'id="dash"' in partial
    assert "<html" not in partial
    env.api.delete(f"/schedules/{schedule['id']}")


def test_dashboard_is_the_start_page() -> None:
    me = ensure_local_user(unique("start"), ["viewer"])
    ui = login_ui(me.username, me.password)
    resp = ui.get(f"{API_URL}/ui")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/dashboard"
