"""Regressions for filtered ownership and deep same-task durable backlogs."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from apscheduler.events import JobEvent

import infrastructure.scheduling.scheduler.apscheduler_executor as scheduler_executor
from infrastructure.scheduling.scheduler.apscheduler_executor import ScheduledThreadPoolExecutor


class _Scheduler:
    def __init__(self, jobs: list[SimpleNamespace]) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.event_codes: list[int] = []

    def _create_lock(self) -> threading.RLock:
        return threading.RLock()

    def _dispatch_event(self, event: JobEvent) -> None:
        self.event_codes.append(event.code)

    def get_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs.values())

    def get_job(self, job_id: str) -> SimpleNamespace | None:
        return self.jobs.get(job_id)


def _job(task_id: str, runners: object) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_instances=1,
        misfire_grace_time=None,
        func=lambda **_kwargs: None,
        args=(task_id, runners),
        kwargs={},
        _jobstore_alias="default",
        trigger=SimpleNamespace(),
    )


def _silence_observability(monkeypatch: pytest.MonkeyPatch, pending: list[SimpleNamespace]) -> None:
    monkeypatch.setattr(scheduler_executor, "_task_is_enabled", lambda _task_id: True)
    monkeypatch.setattr(
        scheduler_executor,
        "_backlog_snapshot",
        lambda _eligible: SimpleNamespace(
            pending_count=len(pending),
            oldest_pending_age_seconds=0.0 if pending else None,
        ),
    )
    monkeypatch.setattr(scheduler_executor, "_record_backlog_state", lambda *_args, **_kwargs: None)


def test_resync_removed_recurring_job_is_not_drained_by_old_filtered_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task removed by filtered resync remains durable for its new owner."""
    runners = object()
    first = _job("task-first", runners)
    moved = _job("task-moved", runners)
    scheduler = _Scheduler([first, moved])
    pending: list[SimpleNamespace] = []
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    moved_started = threading.Event()

    def on_submit(task_id: str, scheduled_run_time: datetime) -> None:
        with lock:
            pending.append(
                SimpleNamespace(task_id=task_id, fire_time=scheduled_run_time.isoformat())
            )

    def recoverable_runs(
        eligible_task_ids: set[str], *, limit: int
    ) -> list[SimpleNamespace]:
        with lock:
            return [run for run in pending if run.task_id in eligible_task_ids][:limit]

    def execute_recoverable(run: SimpleNamespace, _runners: object) -> list[object]:
        with lock:
            pending.remove(run)
        if run.task_id == first.id:
            first_started.set()
            assert release_first.wait(10)
        else:
            moved_started.set()
        return []

    monkeypatch.setattr(scheduler_executor, "_recoverable_runs", recoverable_runs)
    monkeypatch.setattr(scheduler_executor, "_execute_recoverable_run", execute_recoverable)
    _silence_observability(monkeypatch, pending)

    executor = ScheduledThreadPoolExecutor(max_workers=1, on_submit=on_submit)
    executor.start(scheduler, "default")
    now = datetime.now(UTC)
    try:
        executor.submit_job(first, [now])
        assert first_started.wait(10)
        executor.submit_job(moved, [now + timedelta(seconds=1)])

        # Simulate resync removing a task that no longer matches this scheduler's
        # task_filter. Its durable row must not be claimed by this old owner.
        scheduler.jobs.pop(moved.id)
        release_first.set()

        assert not moved_started.wait(0.5)
        with lock:
            assert [run.task_id for run in pending] == [moved.id]
    finally:
        release_first.set()
        executor.shutdown(wait=True)


def test_deep_same_task_prefix_does_not_hide_other_dispatchable_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows beyond the first 1,000 are found when the prefix belongs to one task."""
    runners = object()
    first = _job("task-a", runners)
    later = _job("task-b", runners)
    scheduler = _Scheduler([first, later])
    pending = [
        SimpleNamespace(task_id=first.id, fire_time=f"2026-09-17T12:{index // 60:02d}:{index % 60:02d}Z")
        for index in range(1_000)
    ]
    pending.append(SimpleNamespace(task_id=later.id, fire_time="2026-09-18T05:00:00Z"))
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()
    started: list[str] = []

    def recoverable_runs(
        eligible_task_ids: set[str], *, limit: int
    ) -> list[SimpleNamespace]:
        with lock:
            return [run for run in pending if run.task_id in eligible_task_ids][:limit]

    def execute_recoverable(run: SimpleNamespace, _runners: object) -> list[object]:
        with lock:
            pending.remove(run)
            started.append(run.task_id)
            if set(started) == {first.id, later.id}:
                both_started.set()
        assert release.wait(10)
        return []

    monkeypatch.setattr(scheduler_executor, "_recoverable_runs", recoverable_runs)
    monkeypatch.setattr(scheduler_executor, "_execute_recoverable_run", execute_recoverable)
    _silence_observability(monkeypatch, pending)

    executor = ScheduledThreadPoolExecutor(max_workers=2, on_submit=lambda *_args: None)
    executor.start(scheduler, "default")
    recovery_job = SimpleNamespace(id="scheduler-claim-recovery", max_instances=1)
    try:
        executor.submit_job(recovery_job, [datetime.now(UTC)])
        assert both_started.wait(10)
        assert set(started) == {first.id, later.id}
        assert executor.in_memory_user_callbacks == 2
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert executor.peak_in_memory_user_callbacks == 2
