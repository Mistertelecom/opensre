"""Regressions for one-shot ownership and durable retry backoff."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from apscheduler.events import JobEvent
from apscheduler.triggers.date import DateTrigger

from infrastructure.scheduling.scheduler import apscheduler_executor as scheduler_executor


class _Scheduler:
    def __init__(self, jobs: list[SimpleNamespace] | None = None) -> None:
        self.jobs = {job.id: job for job in jobs or []}
        self.event_codes: list[int] = []

    def _create_lock(self) -> threading.RLock:
        return threading.RLock()

    def _dispatch_event(self, event: JobEvent) -> None:
        self.event_codes.append(event.code)

    def get_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs.values())

    def get_job(self, job_id: str) -> SimpleNamespace | None:
        return self.jobs.get(job_id)


def _job(
    task_id: str,
    runners: object,
    *,
    trigger: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_instances=1,
        misfire_grace_time=None,
        func=lambda **_kwargs: None,
        args=(task_id, runners),
        kwargs={},
        _jobstore_alias="default",
        trigger=trigger or SimpleNamespace(),
    )


def _silence_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scheduler_executor,
        "_backlog_snapshot",
        lambda _eligible: SimpleNamespace(
            pending_count=0,
            oldest_pending_age_seconds=None,
        ),
    )
    monkeypatch.setattr(
        scheduler_executor,
        "_record_backlog_state",
        lambda *_args, **_kwargs: None,
    )


def test_one_shot_hint_is_exact_and_retires_after_terminal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed DateTrigger admission cannot authorize a later fire time."""
    runners = object()
    fire_at = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    first_key = scheduler_executor._durable_fire_time(fire_at)
    later_key = scheduler_executor._durable_fire_time(fire_at + timedelta(minutes=5))
    job = _job(
        "task-once",
        runners,
        trigger=DateTrigger(run_date=fire_at),
    )
    scheduler = _Scheduler()
    pending: list[SimpleNamespace] = []
    executed: list[str] = []
    terminal_checked = threading.Event()
    later_executed = threading.Event()

    def on_submit(task_id: str, scheduled_run_time: datetime) -> None:
        pending.append(
            SimpleNamespace(
                task_id=task_id,
                fire_time=scheduler_executor._durable_fire_time(scheduled_run_time),
            )
        )

    def recoverable_runs(
        eligible_task_ids: set[str],
        *,
        limit: int,
    ) -> list[SimpleNamespace]:
        return [run for run in pending if run.task_id in eligible_task_ids][:limit]

    def execute_recoverable(run: SimpleNamespace, _runners: object) -> list[object]:
        pending.remove(run)
        executed.append(run.fire_time)
        if run.fire_time == later_key:
            later_executed.set()
        return []

    def run_is_terminal(task_id: str, fire_time: str) -> bool:
        assert task_id == job.id
        if fire_time == first_key:
            terminal_checked.set()
            return True
        return False

    monkeypatch.setattr(scheduler_executor, "_enabled_task_ids", lambda: {job.id})
    monkeypatch.setattr(scheduler_executor, "_recoverable_runs", recoverable_runs)
    monkeypatch.setattr(scheduler_executor, "_execute_recoverable_run", execute_recoverable)
    monkeypatch.setattr(scheduler_executor, "_durable_run_is_terminal", run_is_terminal)
    _silence_observability(monkeypatch)

    executor = scheduler_executor.ScheduledThreadPoolExecutor(
        max_workers=1,
        on_submit=on_submit,
    )
    executor.start(scheduler, "default")
    recovery_job = SimpleNamespace(id="scheduler-claim-recovery", max_instances=1)
    try:
        executor.submit_job(job, [fire_at])
        assert terminal_checked.wait(5)
        assert executed == [first_key]

        pending.append(SimpleNamespace(task_id=job.id, fire_time=later_key))
        executor.submit_job(recovery_job, [fire_at + timedelta(minutes=6)])
        assert not later_executed.wait(0.5)
        assert executed == [first_key]
        assert [run.fire_time for run in pending] == [later_key]
    finally:
        executor.shutdown(wait=True)


def test_durable_exception_defers_task_until_periodic_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-claim failure cannot hot-loop the same pending task."""
    runners = object()
    job = _job("task-retry", runners)
    scheduler = _Scheduler([job])
    run = SimpleNamespace(
        task_id=job.id,
        fire_time="2026-09-17T12:00:00Z",
    )
    pending = [run]
    attempts: list[str] = []
    first_attempt = threading.Event()
    second_attempt = threading.Event()

    def recoverable_runs(
        eligible_task_ids: set[str],
        *,
        limit: int,
    ) -> list[SimpleNamespace]:
        return [item for item in pending if item.task_id in eligible_task_ids][:limit]

    def execute_recoverable(item: SimpleNamespace, _runners: object) -> list[object]:
        attempts.append(item.fire_time)
        if len(attempts) == 1:
            first_attempt.set()
            raise RuntimeError("transient storage failure")
        pending.remove(item)
        second_attempt.set()
        return []

    monkeypatch.setattr(scheduler_executor, "_enabled_task_ids", lambda: {job.id})
    monkeypatch.setattr(scheduler_executor, "_recoverable_runs", recoverable_runs)
    monkeypatch.setattr(scheduler_executor, "_execute_recoverable_run", execute_recoverable)
    _silence_observability(monkeypatch)

    executor = scheduler_executor.ScheduledThreadPoolExecutor(
        max_workers=1,
        on_submit=lambda *_args: None,
    )
    executor.start(scheduler, "default")
    recovery_job = SimpleNamespace(id="scheduler-claim-recovery", max_instances=1)
    try:
        executor.submit_job(recovery_job, [datetime.now(UTC)])
        assert first_attempt.wait(5)

        for _ in range(100):
            with executor._dispatch_lock:
                deferred = job.id in executor._deferred_task_ids
            if deferred:
                break
            threading.Event().wait(0.01)
        assert deferred
        threading.Event().wait(0.1)
        assert attempts == [run.fire_time]
        assert pending == [run]

        executor.submit_job(recovery_job, [datetime.now(UTC) + timedelta(minutes=1)])
        assert second_attempt.wait(5)
        assert attempts == [run.fire_time, run.fire_time]
        assert not pending
    finally:
        executor.shutdown(wait=True)
