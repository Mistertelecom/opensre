"""Exact-key durable recovery lookups for transient APScheduler ownership hints."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from infrastructure.scheduling.scheduler.storage import database
from infrastructure.scheduling.scheduler.storage.run_store import RecoverableRun
from infrastructure.scheduling.scheduler.types import TaskStatus


def get_recoverable_runs_for_keys(
    run_keys: Collection[tuple[str, str]],
) -> list[RecoverableRun]:
    """Return recoverable rows for exact durable keys, filtering before any limit.

    One-shot DateTrigger ownership is scoped to an exact ``(task_id, fire_time)``
    key. Looking up those keys directly prevents older pending rows for the same
    task from hiding the admitted one-shot behind the generic recovery batch
    limit, while preserving the same live-owner and expired-lease rules as the
    normal recovery query.
    """
    if not run_keys:
        return []

    now_text = datetime.now(UTC).isoformat()
    rows: list[tuple[str, str, str]] = []
    with database.connection() as conn:
        for task_id, fire_time in run_keys:
            row = conn.execute(
                "SELECT current.task_id, current.fire_time, current.started_at "
                "FROM task_runs AS current "
                "WHERE current.task_id = ? AND current.fire_time = ? "
                "AND (current.status = ? OR (current.status = ? "
                "AND current.lease_expires_at != '' AND current.lease_expires_at < ?)) "
                "AND NOT EXISTS (SELECT 1 FROM task_runs AS live "
                "WHERE live.task_id = current.task_id AND live.status = ? "
                "AND live.lease_expires_at >= ?) "
                "AND current.attempt = (SELECT MAX(latest.attempt) FROM task_runs AS latest "
                "WHERE latest.task_id = current.task_id "
                "AND latest.fire_time = current.fire_time) "
                "ORDER BY current.attempt DESC LIMIT 1",
                (
                    task_id,
                    fire_time,
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    now_text,
                    TaskStatus.RUNNING.value,
                    now_text,
                ),
            ).fetchone()
            if row is not None:
                rows.append((str(row[0]), str(row[1]), str(row[2])))

    rows.sort(key=lambda row: (row[2], row[0], row[1]))
    return [RecoverableRun(task_id=row[0], fire_time=row[1]) for row in rows]


__all__ = ["get_recoverable_runs_for_keys"]
