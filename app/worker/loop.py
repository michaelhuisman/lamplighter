import logging
import shutil
import signal
import threading
from functools import partial
from types import FrameType

import psycopg
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.db import get_sessionmaker
from app.core.openbao import get_openbao
from app.services import queue
from app.worker.credentials import (
    CredentialResolver,
    OpenBaoResolver,
    OpenBaoRunTokens,
    RunTokenIssuer,
    UnconfiguredResolver,
)
from app.worker.executor import Executor
from app.worker.galaxy import CollectionCache
from app.worker.git import RepoCache
from app.worker.locking import PgTemplateLocker

log = logging.getLogger(__name__)


def _resolver() -> CredentialResolver:
    client = get_openbao()
    if client is None:
        log.warning("OpenBao is not configured: runs that need credentials will fail")
        return UnconfiguredResolver()
    return OpenBaoResolver(client)


def _run_tokens(settings: Settings) -> RunTokenIssuer | None:
    client = get_openbao()
    if client is None or not settings.openbao_run_token_role:
        return None
    return OpenBaoRunTokens(client, settings.openbao_run_token_role)


def recover(settings: Settings, sm: sessionmaker[Session], repos: RepoCache) -> None:
    """Clean up after a crash or hard stop of a previous worker instance."""
    with sm() as session:
        orphaned = queue.fail_orphaned(session, settings.worker_id)
    if orphaned:
        log.warning("marked orphaned runs as error", extra={"run_ids": orphaned})

    # The runtime dir may be shared with other workers: leave their running runs alone.
    dirs = {int(p.name): p for p in settings.runtime_dir.iterdir() if p.name.isdigit()}
    with sm() as session:
        keep = queue.active_elsewhere(session, list(dirs), settings.worker_id)
    for run_id, path in dirs.items():
        if run_id not in keep:
            shutil.rmtree(path, ignore_errors=True)
            log.info("removed stale run dir", extra={"run_id": run_id})
    repos.prune_all()


def run_worker(settings: Settings) -> int:
    stop = threading.Event()

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("stop requested, finishing current run", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    sm = get_sessionmaker()
    repos = RepoCache(settings.repo_cache_dir)
    collections = CollectionCache(
        settings.collections_cache_dir, settings.collections_install_timeout_s
    )
    executor = Executor(
        settings,
        sm,
        _resolver(),
        repos,
        collections=collections,
        run_tokens=_run_tokens(settings),
    )
    locker = PgTemplateLocker(settings.worker_id)

    recover(settings, sm, repos)
    pruned = collections.prune(settings.collections_cache_days)
    if pruned:
        log.info("pruned collection cache", extra={"entries": pruned})
    log.info("worker started", extra={"worker_id": settings.worker_id})

    while not stop.is_set():
        try:
            with sm() as session:
                claimed = queue.claim(session, settings.worker_id, locker)
            if claimed is None:
                stop.wait(settings.poll_interval_s)
                continue
            log.info("claimed run", extra={"run_id": claimed.run_id})
            try:
                executor.execute(
                    claimed.run_id,
                    lock_guard=partial(locker.ensure, claimed.template_id),
                )
            finally:
                locker.unlock(claimed.template_id)
        except (OperationalError, psycopg.Error):
            log.exception("database unavailable, retrying")
            locker.reset()
            stop.wait(settings.poll_interval_s)

    log.info("worker stopped")
    return 0
