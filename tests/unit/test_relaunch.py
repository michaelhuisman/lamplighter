"""Which hosts a "relaunch failed hosts" targets."""

from types import SimpleNamespace

from app.services.runs import failed_hosts


def _run(stats: dict[str, dict[str, int]] | None) -> SimpleNamespace:
    return SimpleNamespace(stats=stats)


def test_failed_and_unreachable_hosts() -> None:
    run = _run(
        {
            "ok": {"a": 3, "b": 2, "c": 1},
            "failed": {"b": 1, "d": 0},
            "unreachable": {"c": 1},
            "skipped": {"a": 1},
        }
    )
    assert failed_hosts(run) == ["b", "c"]  # type: ignore[arg-type]


def test_no_stats_or_no_failures() -> None:
    assert failed_hosts(_run(None)) == []  # type: ignore[arg-type]
    assert failed_hosts(_run({"ok": {"a": 1}, "failed": {}, "unreachable": {}})) == []  # type: ignore[arg-type]
