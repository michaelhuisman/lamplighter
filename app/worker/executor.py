"""Execution of one claimed run with ansible-runner."""

import logging
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import ansible_runner
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Credential, Inventory, Project, Run, RunStatus, Template
from app.services import queue
from app.services.events import SecretMasker, filter_event
from app.worker.credentials import (
    CredentialError,
    CredentialRef,
    CredentialResolver,
    resolve,
    resolve_git,
)
from app.worker.galaxy import CollectionCache, Collections, CollectionsError
from app.worker.git import GitError, RepoCache

log = logging.getLogger(__name__)

EVENT_BATCH_SIZE = 50
# Message from ssh-add (via ansible-runner): contains the internal path and the key comment.
_SSH_AGENT_NOISE = "Identity added: "
EVENT_FLUSH_INTERVAL_S = 1.0
CANCEL_CHECK_INTERVAL_S = 1.0
LOCK_CHECK_INTERVAL_S = 5.0
LOCK_LOST_REASON = "overlap lock lost"
# Lamplighter's own setup notes in the run log, before ansible-runner's events (seq >= 1).
SETUP_NOTE_SEQ = 0
SETUP_NOTE_EVENT = "lamplighter_note"

# Checks whether the run's overlap lock still belongs to this worker.
LockGuard = Callable[[], bool]

# ansible_runner.run(**kwargs) -> Runner; untyped library.
RunnerFn = Callable[..., Any]

_RUNNER_STATUS = {
    "successful": RunStatus.SUCCESSFUL,
    "failed": RunStatus.FAILED,
    "timeout": RunStatus.TIMEOUT,
    "canceled": RunStatus.CANCELED,
}


class RunSetupError(Exception):
    pass


@dataclass(frozen=True)
class RunSpec:
    run_id: int
    project_id: int
    git_url: str
    branch: str
    playbook_path: str
    inventory_source: str
    inventory_path: str | None
    inventory_content: str | None
    extra_vars: dict[str, Any]  # free-form JSON
    limit: str | None
    tags: str | None
    skip_tags: str | None
    verbosity: int
    timeout_s: int | None
    machine_credential: CredentialRef
    vault_credential: CredentialRef | None
    git_credential: CredentialRef | None = None
    known_hosts_credential: CredentialRef | None = None


def _ref(cred: Credential) -> CredentialRef:
    return CredentialRef(
        id=cred.id, type=cred.type, openbao_path=cred.openbao_path, openbao_key=cred.openbao_key
    )


def load_spec(session: Session, run_id: int) -> RunSpec:
    with session.begin():
        run = session.get_one(Run, run_id)
        if run.template_id is None:
            raise RunSetupError(queue.TEMPLATE_DELETED)
        template = session.get_one(Template, run.template_id)
        project = session.get_one(Project, template.project_id)
        inventory = session.get_one(Inventory, template.inventory_id)
        machine = session.get_one(Credential, template.machine_credential_id)
        vault = (
            session.get_one(Credential, template.vault_credential_id)
            if template.vault_credential_id is not None
            else None
        )
        if machine.type != "ssh_key":
            raise RunSetupError(f"machine credential {machine.id} is not of type ssh_key")
        if vault is not None and vault.type != "vault_password":
            raise RunSetupError(f"vault credential {vault.id} is not of type vault_password")
        git = (
            session.get_one(Credential, project.credential_id)
            if project.credential_id is not None
            else None
        )
        if git is not None and git.type != "git_token":
            raise RunSetupError(f"project credential {git.id} is not of type git_token")
        known_hosts = (
            session.get_one(Credential, template.known_hosts_credential_id)
            if template.known_hosts_credential_id is not None
            else None
        )
        if known_hosts is not None and known_hosts.type != "known_hosts":
            raise RunSetupError(f"credential {known_hosts.id} is not of type known_hosts")
        return RunSpec(
            run_id=run.id,
            project_id=project.id,
            git_url=project.git_url,
            branch=project.branch,
            playbook_path=template.playbook_path,
            inventory_source=inventory.source_type,
            inventory_path=inventory.path,
            inventory_content=inventory.content,
            extra_vars=dict(run.extra_vars),
            limit=run.limit,
            tags=template.tags,
            skip_tags=template.skip_tags,
            verbosity=template.verbosity,
            timeout_s=template.timeout_s,
            machine_credential=_ref(machine),
            vault_credential=_ref(vault) if vault else None,
            git_credential=_ref(git) if git else None,
            known_hosts_credential=_ref(known_hosts) if known_hosts else None,
        )


def _inside(base: Path, relative: str) -> Path:
    """Resolve a relative path within `base`; reject absolute paths and '..'."""
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RunSetupError(f"path must be relative without '..': {relative!r}")
    return base / rel


class EventSink:
    """Filters events and writes them in batches."""

    def __init__(self, sm: sessionmaker[Session], run_id: int, masker: SecretMasker) -> None:
        self._sm = sm
        self._run_id = run_id
        self._masker = masker
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        # ansible-runner reads stats from disk, but we don't write events there.
        self.stats: dict[str, Any] | None = None

    def handle(self, raw: Mapping[str, Any]) -> bool:
        if raw.get("event") == "playbook_on_stats":
            self.stats = _normalize_stats(raw.get("event_data"))
        if raw.get("event") == "verbose" and str(raw.get("stdout", "")).startswith(
            _SSH_AGENT_NOISE
        ):
            return False
        event = filter_event(raw, self._masker)
        if event is not None:
            self._buffer.append(event.as_row(self._run_id))
        if (
            len(self._buffer) >= EVENT_BATCH_SIZE
            or time.monotonic() - self._last_flush >= EVENT_FLUSH_INTERVAL_S
        ):
            try:
                self.flush()
            except Exception:
                # The buffer stays; the next flush retries.
                log.warning("event flush failed, will retry", exc_info=True)
        # False: ansible-runner does not write the (unfiltered) event to disk.
        return False

    def flush(self) -> None:
        if self._buffer:
            with self._sm() as session:
                queue.add_events(session, self._buffer)
            self._buffer = []
        self._last_flush = time.monotonic()


class CancelCheck:
    """ansible-runner's cancel_callback: cancel on request, or if the overlap lock is lost
    for good (another run of the same template may already be running)."""

    def __init__(
        self, sm: sessionmaker[Session], run_id: int, lock_guard: LockGuard | None = None
    ) -> None:
        self._sm = sm
        self._run_id = run_id
        self._lock_guard = lock_guard
        self._last_check = 0.0
        self._last_lock_check = time.monotonic()
        self._canceled = False
        self.lost_lock = False

    def __call__(self) -> bool:
        if self._canceled:
            return True
        now = time.monotonic()
        if self._lock_guard is not None and now - self._last_lock_check >= LOCK_CHECK_INTERVAL_S:
            self._last_lock_check = now
            if not self._lock_guard():
                log.error("overlap lock lost, aborting run", extra={"run_id": self._run_id})
                self.lost_lock = self._canceled = True
                return True
        if now - self._last_check < CANCEL_CHECK_INTERVAL_S:
            return False
        self._last_check = now
        try:
            with self._sm() as session:
                self._canceled = queue.is_cancel_requested(session, self._run_id)
        except Exception:
            log.warning("cancel check failed", exc_info=True)
        return self._canceled


def _normalize_stats(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    mapping = {
        "ok": "ok",
        "changed": "changed",
        "failed": "failures",
        "unreachable": "dark",
        "skipped": "skipped",
        "rescued": "rescued",
        "ignored": "ignored",
    }
    return {ours: dict(raw.get(theirs) or {}) for ours, theirs in mapping.items()}


class Executor:
    def __init__(
        self,
        settings: Settings,
        sm: sessionmaker[Session],
        resolver: CredentialResolver,
        repos: RepoCache,
        runner_fn: RunnerFn = ansible_runner.run,
        collections: CollectionCache | None = None,
    ) -> None:
        self._settings = settings
        self._sm = sm
        self._resolver = resolver
        self._repos = repos
        self._runner_fn = runner_fn
        self._collections = collections

    def run_dir(self, run_id: int) -> Path:
        return self._settings.runtime_dir / str(run_id)

    def execute(self, run_id: int, lock_guard: LockGuard | None = None) -> RunStatus:
        run_dir = self.run_dir(run_id)
        check = CancelCheck(self._sm, run_id, lock_guard)
        spec: RunSpec | None = None
        status = RunStatus.ERROR
        rc: int | None = None
        stats: dict[str, Any] | None = None
        reason: str | None = None
        try:
            with self._sm() as session:
                spec = load_spec(session, run_id)
            run_dir.mkdir(mode=0o700, parents=False)
            status, rc, stats = self._run(spec, run_dir, check)
            if check.lost_lock:
                status, reason = RunStatus.ERROR, LOCK_LOST_REASON
            elif status == RunStatus.CANCELED:
                reason = "canceled by user"
            elif status == RunStatus.TIMEOUT:
                reason = f"exceeded timeout of {spec.timeout_s}s"
        except (RunSetupError, CredentialError, GitError, CollectionsError) as exc:
            reason = str(exc)
            log.warning("run setup failed", extra={"run_id": run_id, "reason": reason})
        except Exception as exc:
            reason = f"internal error: {type(exc).__name__}"
            log.exception("run failed with exception", extra={"run_id": run_id})
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
            if spec is not None:
                try:
                    self._repos.prune(spec.project_id)
                except Exception:
                    log.warning("worktree prune failed", exc_info=True)
            with self._sm() as session:
                queue.finish(
                    session,
                    run_id,
                    status=status,
                    rc=rc,
                    stats=stats,
                    reason=reason,
                )
            log.info("run finished", extra={"run_id": run_id, "status": status, "rc": rc})
        return status

    def _run(
        self, spec: RunSpec, run_dir: Path, check: CancelCheck
    ) -> tuple[RunStatus, int | None, dict[str, Any] | None]:
        project_dir = run_dir / "project"
        secrets: list[str] = []
        git_auth = None
        if spec.git_credential is not None:
            git_auth = resolve_git(self._resolver, spec.git_credential)
            secrets.append(git_auth.token)
        try:
            sha = self._repos.checkout(
                spec.project_id,
                spec.git_url,
                spec.branch,
                project_dir,
                auth=git_auth,
                auth_dir=run_dir / "git-auth",
            )
        except GitError as exc:
            # git output could in theory contain the token; mask before storing.
            raise GitError(SecretMasker(secrets).mask(str(exc))) from None
        with self._sm() as session:
            queue.set_commit(session, spec.run_id, sha)
        collections = self._install_collections(spec.run_id, project_dir, SecretMasker(secrets))

        playbook = _inside(project_dir, spec.playbook_path)
        if spec.inventory_source == "inline":
            inventory = run_dir / "inventory" / "hosts"
            inventory.parent.mkdir(mode=0o700)
            inventory.write_text(spec.inventory_content or "")
        else:
            inventory = _inside(project_dir, spec.inventory_path or "")

        ssh_key = resolve(self._resolver, spec.machine_credential)
        if not ssh_key.endswith("\n"):
            ssh_key += "\n"
        secrets.append(ssh_key)

        cmdline: str | None = None
        if spec.vault_credential is not None:
            vault_password = resolve(self._resolver, spec.vault_credential)
            secrets.append(vault_password)
            vault_file = run_dir / "vault_password"
            vault_file.touch(mode=0o600)
            vault_file.write_text(vault_password)
            cmdline = f"--vault-password-file {shlex.quote(str(vault_file))}"

        envvars = {
            "ANSIBLE_HOST_KEY_CHECKING": str(self._settings.ansible_host_key_checking),
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            # Own ControlPath per run: SSH master connections (ControlPersist) are never shared
            # between runs. Otherwise a run could reuse a connection that another run set up
            # without (or with a different) host key check.
            "ANSIBLE_SSH_CONTROL_PATH_DIR": str(run_dir / "cp"),
        }
        envvars.update(self._host_key_env(spec, run_dir))
        if collections is not None:
            envvars["ANSIBLE_COLLECTIONS_PATH"] = collections.search_path()

        sink = EventSink(self._sm, spec.run_id, SecretMasker(secrets))
        runner = self._runner_fn(
            private_data_dir=str(run_dir),
            project_dir=str(project_dir),
            playbook=str(playbook.relative_to(project_dir)),
            inventory=str(inventory),
            extravars=spec.extra_vars,
            limit=spec.limit,
            tags=spec.tags,
            skip_tags=spec.skip_tags,
            verbosity=spec.verbosity or None,
            ssh_key=ssh_key,
            cmdline=cmdline,
            envvars=envvars,
            timeout=spec.timeout_s,
            settings={"pexpect_timeout": 1},
            event_handler=sink.handle,
            cancel_callback=check,
            process_isolation=False,
            suppress_env_files=True,
            quiet=True,
        )
        sink.flush()
        status = _RUNNER_STATUS.get(str(runner.status), RunStatus.ERROR)
        rc = runner.rc if isinstance(runner.rc, int) else None
        return status, rc, sink.stats

    def _install_collections(
        self, run_id: int, project_dir: Path, masker: SecretMasker
    ) -> Collections | None:
        """Collections from the project's collections/requirements.yml, with a note in the
        run log. A requirements entry could contain a token in a URL: mask errors."""
        if self._collections is None:
            return None
        try:
            collections = self._collections.ensure(project_dir)
        except CollectionsError as exc:
            raise CollectionsError(masker.mask(str(exc))) from None
        if collections is not None:
            how = "cached" if collections.cached else f"installed in {collections.seconds:.0f}s"
            self._note(run_id, f"Collections from collections/requirements.yml: {how}")
        return collections

    def _note(self, run_id: int, text: str) -> None:
        row = {
            "run_id": run_id,
            "seq": SETUP_NOTE_SEQ,
            "event": SETUP_NOTE_EVENT,
            "host": None,
            "task": None,
            "created_at": datetime.now(UTC),
            "stdout": text,
            "data": {},
        }
        with self._sm() as session:
            queue.add_events(session, [row])

    def _host_key_env(self, spec: RunSpec, run_dir: Path) -> dict[str, str]:
        """Host key checking: a known_hosts credential on the template enforces strict
        checking, regardless of the global setting. Without known_hosts the global
        setting applies; if that is on, we refuse the run right away with a clear reason."""
        if spec.known_hosts_credential is None:
            if self._settings.ansible_host_key_checking:
                raise RunSetupError(
                    "host key checking is enabled but the template has no known_hosts credential"
                )
            return {}
        known_hosts = resolve(self._resolver, spec.known_hosts_credential)
        path = run_dir / "known_hosts"
        path.touch(mode=0o600)
        path.write_text(known_hosts if known_hosts.endswith("\n") else known_hosts + "\n")
        return {
            "ANSIBLE_HOST_KEY_CHECKING": "True",
            "ANSIBLE_SSH_COMMON_ARGS": " ".join(
                [
                    f"-o UserKnownHostsFile={shlex.quote(str(path))}",
                    "-o GlobalKnownHostsFile=/dev/null",
                    "-o StrictHostKeyChecking=yes",
                ]
            ),
        }
