"""Executor tests without a database and without a real ansible-runner."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.models import RunStatus
from app.worker import executor as executor_mod
from app.worker.credentials import CredentialError, CredentialRef, GitAuth
from app.worker.executor import SETUP_NOTE_SEQ, Executor, RunSpec
from app.worker.galaxy import Collections, CollectionsError
from app.worker.git import GitError

SHA = "a" * 40


class FakeRepos:
    def __init__(self) -> None:
        self.pruned: list[int] = []
        self.auth: GitAuth | None = None
        self.fail_with: str | None = None

    def checkout(
        self,
        project_id: int,
        git_url: str,
        branch: str,
        dest: Path,
        *,
        auth: GitAuth | None = None,
        auth_dir: Path | None = None,
    ) -> str:
        self.auth = auth
        if self.fail_with:
            raise GitError(self.fail_with)
        dest.mkdir()
        (dest / "ping.yml").write_text("- hosts: all\n")
        return SHA

    def prune(self, project_id: int) -> None:
        self.pruned.append(project_id)


class FakeResolver:
    def __init__(self, secrets: dict[str, dict[str, str]]) -> None:
        self.secrets = secrets

    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        if ref.openbao_path not in self.secrets:
            raise CredentialError(f"credential {ref.id}: secret not found: {ref.openbao_path}")
        return self.secrets[ref.openbao_path]


class Recorder:
    def __init__(self) -> None:
        self.finished: dict[str, Any] = {}
        self.commit: str | None = None
        self.events: list[dict[str, Any]] = []


@contextmanager
def _dummy_session() -> Iterator[None]:
    yield None


def _spec(**overrides: Any) -> RunSpec:
    values: dict[str, Any] = {
        "run_id": 5,
        "project_id": 1,
        "git_url": "file:///repo.git",
        "branch": "main",
        "playbook_path": "ping.yml",
        "inventory_source": "inline",
        "inventory_path": None,
        "inventory_content": "host1\n",
        "extra_vars": {"a": 1},
        "limit": None,
        "tags": None,
        "skip_tags": None,
        "verbosity": 0,
        "timeout_s": 30,
        "machine_credential": CredentialRef(1, "ssh_key", "ssh", "key"),
        "vault_credential": None,
    }
    values.update(overrides)
    return RunSpec(**values)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    resolver = FakeResolver(
        {
            "ssh": {"key": "PRIVATE-KEY-CONTENT-123\n"},
            "vault": {"pw": "vault-password-456"},
            "git/repo": {"token": "GIT-TOKEN-789", "username": "deploy"},
        }
    )

    rec = Recorder()
    spec_holder: dict[str, RunSpec] = {"spec": _spec()}
    monkeypatch.setattr(executor_mod, "load_spec", lambda _s, _id: spec_holder["spec"])
    monkeypatch.setattr(
        executor_mod.queue, "set_commit", lambda _s, _id, sha: setattr(rec, "commit", sha)
    )
    monkeypatch.setattr(executor_mod.queue, "finish", lambda _s, _id, **kw: rec.finished.update(kw))
    monkeypatch.setattr(executor_mod.queue, "add_events", lambda _s, rows: rec.events.extend(rows))

    settings = Settings(
        database_url="postgresql+psycopg://x/y",
        runtime_dir=runtime,
        ansible_host_key_checking=False,
    )
    repos = FakeRepos()

    def make(runner_fn: Any, collections: Any = None, run_tokens: Any = None) -> Executor:
        return Executor(
            settings,
            _dummy_session,  # type: ignore[arg-type]
            resolver,
            repos,  # type: ignore[arg-type]
            runner_fn=runner_fn,
            collections=collections,
            run_tokens=run_tokens,
        )

    return SimpleNamespace(runtime=runtime, rec=rec, make=make, spec=spec_holder, repos=repos)


def _runner_result(status: str, rc: int) -> SimpleNamespace:
    return SimpleNamespace(status=status, rc=rc)


def test_successful_run(env: SimpleNamespace) -> None:
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        # The private data dir exists during the run.
        assert Path(kwargs["private_data_dir"]).is_dir()
        assert Path(kwargs["inventory"]).read_text() == "host1\n"
        kwargs["event_handler"](
            {
                "counter": 1,
                "event": "playbook_on_stats",
                "stdout": "",
                "event_data": {"ok": {"host1": 1}, "changed": {}, "failures": {}, "dark": {}},
            }
        )
        return _runner_result("successful", 0)

    status = env.make(fake_runner).execute(5)

    assert status == RunStatus.SUCCESSFUL
    assert env.rec.commit == SHA
    assert env.rec.finished["status"] == RunStatus.SUCCESSFUL
    assert env.rec.finished["rc"] == 0
    assert env.rec.finished["stats"]["ok"] == {"host1": 1}
    assert len(env.rec.events) == 1
    assert seen["process_isolation"] is False
    assert seen["suppress_env_files"] is True
    assert seen["ssh_key"] == "PRIVATE-KEY-CONTENT-123\n"
    assert seen["playbook"] == "ping.yml"
    assert seen["extravars"] == {"a": 1}
    assert seen["timeout"] == 30
    assert seen["cmdline"] is None
    # Own SSH ControlPath per run: no shared master connections between runs.
    assert seen["envvars"]["ANSIBLE_SSH_CONTROL_PATH_DIR"] == str(env.runtime / "5" / "cp")
    assert not (env.runtime / "5").exists()
    assert env.repos.pruned == [1]


@pytest.mark.parametrize(
    ("runner_status", "expected"),
    [
        ("failed", RunStatus.FAILED),
        ("timeout", RunStatus.TIMEOUT),
        ("canceled", RunStatus.CANCELED),
        ("something-odd", RunStatus.ERROR),
    ],
)
def test_status_mapping(env: SimpleNamespace, runner_status: str, expected: RunStatus) -> None:
    status = env.make(lambda **_: _runner_result(runner_status, 2)).execute(5)
    assert status == expected
    assert env.rec.finished["status"] == expected
    assert not (env.runtime / "5").exists()


def test_exception_in_runner_cleans_up(env: SimpleNamespace) -> None:
    def boom(**kwargs: Any) -> None:
        (Path(kwargs["private_data_dir"]) / "artifacts").mkdir()
        raise RuntimeError("runner exploded")

    status = env.make(boom).execute(5)

    assert status == RunStatus.ERROR
    assert env.rec.finished["reason"] == "internal error: RuntimeError"
    assert not (env.runtime / "5").exists()
    assert env.repos.pruned == [1]


def test_missing_credential_is_error_and_cleans_up(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(machine_credential=CredentialRef(9, "ssh_key", "nope", "key"))
    status = env.make(lambda **_: pytest.fail("runner must not start")).execute(5)
    assert status == RunStatus.ERROR
    assert "credential 9" in env.rec.finished["reason"]
    assert not (env.runtime / "5").exists()


def test_path_traversal_in_playbook_is_rejected(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(playbook_path="../../etc/passwd")
    status = env.make(lambda **_: pytest.fail("runner must not start")).execute(5)
    assert status == RunStatus.ERROR
    assert "relative" in env.rec.finished["reason"]
    assert not (env.runtime / "5").exists()


def test_vault_password_file(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(vault_credential=CredentialRef(2, "vault_password", "vault", "pw"))
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        vault_file = Path(kwargs["cmdline"].split()[-1])
        assert vault_file.read_text() == "vault-password-456"
        assert vault_file.stat().st_mode & 0o077 == 0
        return _runner_result("successful", 0)

    assert env.make(fake_runner).execute(5) == RunStatus.SUCCESSFUL
    assert seen["cmdline"].startswith("--vault-password-file ")
    assert not (env.runtime / "5").exists()


def test_secrets_are_masked_in_events(env: SimpleNamespace) -> None:
    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        kwargs["event_handler"](
            {"counter": 1, "event": "verbose", "stdout": "leak PRIVATE-KEY-CONTENT-123"}
        )
        return _runner_result("successful", 0)

    env.make(fake_runner).execute(5)
    assert "PRIVATE-KEY-CONTENT-123" not in env.rec.events[0]["stdout"]


def test_git_credential_is_passed_and_masked(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(git_credential=CredentialRef(3, "git_token", "git/repo", "token"))

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        kwargs["event_handler"]({"counter": 1, "event": "verbose", "stdout": "x GIT-TOKEN-789 y"})
        return _runner_result("successful", 0)

    assert env.make(fake_runner).execute(5) == RunStatus.SUCCESSFUL
    assert env.repos.auth == GitAuth("deploy", "GIT-TOKEN-789")
    assert "GIT-TOKEN-789" not in repr(env.repos.auth)
    assert "GIT-TOKEN-789" not in env.rec.events[0]["stdout"]


def test_git_failure_is_setup_error_without_token(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(git_credential=CredentialRef(3, "git_token", "git/repo", "token"))
    env.repos.fail_with = "git fetch failed: auth for GIT-TOKEN-789 rejected"
    status = env.make(lambda **_: pytest.fail("runner must not start")).execute(5)
    assert status == RunStatus.ERROR
    reason = env.rec.finished["reason"]
    assert reason.startswith("git fetch failed")
    assert "GIT-TOKEN-789" not in reason
    assert not (env.runtime / "5").exists()


def test_lost_lock_makes_run_error(env: SimpleNamespace) -> None:
    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        check = kwargs["cancel_callback"]
        check._last_lock_check = 0.0
        assert check() is True  # lock_guard zegt: kwijt
        return _runner_result("canceled", 254)

    status = env.make(fake_runner).execute(5, lock_guard=lambda: False)
    assert status == RunStatus.ERROR
    assert env.rec.finished["reason"] == "overlap lock lost"
    assert not (env.runtime / "5").exists()


# --- collections -----------------------------------------------------------------------


class FakeCollections:
    def __init__(self, result: Collections | None = None, error: str | None = None) -> None:
        self.result = result
        self.error = error
        self.projects: list[Path] = []

    def ensure(self, project_dir: Path) -> Collections | None:
        self.projects.append(project_dir)
        if self.error:
            raise CollectionsError(self.error)
        return self.result


def test_project_collections_go_first_on_the_search_path(env: SimpleNamespace) -> None:
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _runner_result("successful", 0)

    fake = FakeCollections(Collections(Path("/cache/abc"), cached=True, seconds=0.1))
    assert env.make(fake_runner, fake).execute(5) == RunStatus.SUCCESSFUL
    assert fake.projects == [env.runtime / "5" / "project"]
    path = seen["envvars"]["ANSIBLE_COLLECTIONS_PATH"].split(":")
    assert path[0] == "/cache/abc"
    assert path[-1] == "/usr/share/ansible/collections"
    # A setup note before ansible's own events (seq 0).
    note = env.rec.events[0]
    assert (note["seq"], note["event"]) == (SETUP_NOTE_SEQ, "lamplighter_note")
    assert note["stdout"] == "Collections from collections/requirements.yml: cached"


def test_without_requirements_nothing_changes(env: SimpleNamespace) -> None:
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _runner_result("successful", 0)

    assert env.make(fake_runner, FakeCollections(None)).execute(5) == RunStatus.SUCCESSFUL
    assert "ANSIBLE_COLLECTIONS_PATH" not in seen["envvars"]
    assert env.rec.events == []


def test_collection_install_error_is_masked(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(git_credential=CredentialRef(3, "git_token", "git/repo", "token"))
    fake = FakeCollections(error="collection install failed: https://GIT-TOKEN-789@git.x/c.git")
    status = env.make(lambda **_: pytest.fail("runner must not start"), fake).execute(5)
    assert status == RunStatus.ERROR
    reason = env.rec.finished["reason"]
    assert reason.startswith("collection install failed")
    assert "GIT-TOKEN-789" not in reason
    assert not (env.runtime / "5").exists()


# --- run token (ANSIBLE_HASHI_VAULT_TOKEN) ---------------------------------------------

RUN_TOKEN = "s.RUN-TOKEN-abcdef"


class FakeRunTokens:
    def __init__(self, create_error: str | None = None, revoke_error: str | None = None) -> None:
        self.created: list[tuple[int, int]] = []
        self.revoked: list[str] = []
        self.create_error = create_error
        self.revoke_error = revoke_error

    def create(self, run_id: int, ttl_s: int) -> str:
        if self.create_error:
            raise CredentialError(self.create_error)
        self.created.append((run_id, ttl_s))
        return RUN_TOKEN

    def revoke(self, token: str) -> None:
        self.revoked.append(token)
        if self.revoke_error:
            raise CredentialError(self.revoke_error)


def test_run_token_is_passed_masked_and_revoked(env: SimpleNamespace) -> None:
    tokens = FakeRunTokens()
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        assert tokens.revoked == []  # still valid while the playbook runs
        kwargs["event_handler"]({"counter": 1, "event": "verbose", "stdout": f"t={RUN_TOKEN}"})
        return _runner_result("successful", 0)

    assert env.make(fake_runner, run_tokens=tokens).execute(5) == RunStatus.SUCCESSFUL
    assert seen["envvars"]["ANSIBLE_HASHI_VAULT_TOKEN"] == RUN_TOKEN
    assert "ANSIBLE_HASHI_VAULT_ADDR" not in seen["envvars"]
    assert seen["suppress_env_files"] is True
    assert tokens.created == [(5, 30)]  # TTL = the run's timeout
    assert tokens.revoked == [RUN_TOKEN]
    assert RUN_TOKEN not in env.rec.events[0]["stdout"]


def test_run_token_ttl_without_timeout_comes_from_settings(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(timeout_s=None)
    tokens = FakeRunTokens()
    env.make(lambda **_: _runner_result("successful", 0), run_tokens=tokens).execute(5)
    assert tokens.created == [(5, 3600)]


@pytest.mark.parametrize("runner_status", ["failed", "timeout", "canceled"])
def test_run_token_is_revoked_when_the_run_fails(env: SimpleNamespace, runner_status: str) -> None:
    tokens = FakeRunTokens()
    env.make(lambda **_: _runner_result(runner_status, 2), run_tokens=tokens).execute(5)
    assert tokens.revoked == [RUN_TOKEN]


def test_run_token_is_revoked_when_the_runner_raises(env: SimpleNamespace) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("runner exploded")

    tokens = FakeRunTokens()
    assert env.make(boom, run_tokens=tokens).execute(5) == RunStatus.ERROR
    assert tokens.revoked == [RUN_TOKEN]


def test_run_token_create_failure_is_setup_error(env: SimpleNamespace) -> None:
    tokens = FakeRunTokens(create_error="run token: permission denied: token role 'x'")
    status = env.make(lambda **_: pytest.fail("runner must not start"), run_tokens=tokens).execute(
        5
    )
    assert status == RunStatus.ERROR
    assert env.rec.finished["reason"] == "run token: permission denied: token role 'x'"
    assert tokens.revoked == []
    assert not (env.runtime / "5").exists()


def test_run_token_revoke_failure_keeps_status_and_logs_no_token(
    env: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    tokens = FakeRunTokens(revoke_error=f"cannot revoke {RUN_TOKEN}")
    status = env.make(lambda **_: _runner_result("successful", 0), run_tokens=tokens).execute(5)
    assert status == RunStatus.SUCCESSFUL
    records = [r for r in caplog.records if r.getMessage() == "run token revoke failed"]
    assert records
    assert all(RUN_TOKEN not in str(r.__dict__) for r in caplog.records)


def test_without_run_tokens_no_token_env(env: SimpleNamespace) -> None:
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _runner_result("successful", 0)

    env.make(fake_runner).execute(5)
    assert "ANSIBLE_HASHI_VAULT_TOKEN" not in seen["envvars"]
