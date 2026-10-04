from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import ActorDep, SessionDep, require
from app.api.schemas import LaunchIn, RelaunchIn, RunEventOut, RunEventsPage, RunOut
from app.core.auth import Action, Principal
from app.core.db import get_sessionmaker
from app.models import RunStatus
from app.services import runs, stream

READ = [Depends(require(Action.READ))]

router = APIRouter(tags=["runs"])


@router.post(
    "/templates/{template_id}/launch", response_model=RunOut, status_code=status.HTTP_201_CREATED
)
def launch(
    template_id: int,
    body: LaunchIn,
    session: SessionDep,
    user: Annotated[Principal, Depends(require(Action.LAUNCH))],
    actor: ActorDep,
) -> RunOut:
    run = runs.launch(
        session,
        template_id,
        triggered_by=user.triggered_by,
        extra_vars=body.extra_vars,
        limit=body.limit,
        actor=actor,
    )
    return RunOut.model_validate(run)


@router.get("/runs", response_model=list[RunOut], dependencies=READ)
def list_runs(
    session: SessionDep,
    template_id: int | None = None,
    status: RunStatus | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunOut]:
    flt = runs.RunFilter(
        template_id=template_id, status=status, since=since, limit=limit, offset=offset
    )
    return [RunOut.model_validate(r) for r in runs.list_runs(session, flt)]


@router.get("/runs/{run_id}", response_model=RunOut, dependencies=READ)
def get_run(run_id: int, session: SessionDep) -> RunOut:
    return RunOut.model_validate(runs.get(session, run_id))


@router.get("/runs/{run_id}/events", response_model=RunEventsPage, dependencies=READ)
def list_events(
    run_id: int,
    session: SessionDep,
    after_seq: Annotated[int, Query(ge=runs.BEFORE_FIRST)] = runs.BEFORE_FIRST,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> RunEventsPage:
    items = [
        RunEventOut.model_validate(e)
        for e in runs.list_events(session, run_id, after_seq=after_seq, limit=limit)
    ]
    next_after = items[-1].seq if len(items) == limit else None
    return RunEventsPage(items=items, next_after_seq=next_after)


@router.post("/runs/{run_id}/relaunch", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def relaunch(
    run_id: int,
    session: SessionDep,
    user: Annotated[Principal, Depends(require(Action.LAUNCH))],
    actor: ActorDep,
    body: RelaunchIn | None = None,
) -> RunOut:
    """A new run with the original's extra vars and limit (409 while it is still active,
    or with failed_hosts_only when no host failed)."""
    run = runs.relaunch(
        session,
        run_id,
        triggered_by=user.triggered_by,
        failed_hosts_only=body.failed_hosts_only if body else False,
        actor=actor,
    )
    return RunOut.model_validate(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require(Action.CANCEL))],
)
def cancel(run_id: int, session: SessionDep, actor: ActorDep) -> RunOut:
    return RunOut.model_validate(runs.cancel(session, run_id, actor=actor))


@router.get("/runs/{run_id}/stream", dependencies=READ)
def stream_run(
    run_id: int,
    session: SessionDep,
    last_event_id: Annotated[str | None, Header()] = None,
    after_seq: Annotated[int, Query(ge=runs.BEFORE_FIRST)] = runs.BEFORE_FIRST,
) -> StreamingResponse:
    """Live events of a run (SSE). Reconnecting resumes from Last-Event-ID."""
    runs.get(session, run_id)  # 404 before the stream starts
    start = max(after_seq, stream.parse_last_event_id(last_event_id))
    return StreamingResponse(
        stream.run_stream(get_sessionmaker(), run_id, after_seq=start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
