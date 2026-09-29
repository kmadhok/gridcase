"""Server-minted task handles for long-running tool calls (the 2026-07-28 stateless pattern:
the handle is an ordinary tool argument, polled with get_task)."""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class TaskRegistry:
    def __init__(self, max_tasks: int = 1000) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max = max_tasks

    def submit(self, fn: Callable[[], Any], kind: str = "investigate") -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._tasks) >= self._max:
                oldest = sorted(self._tasks, key=lambda k: self._tasks[k]["created_at"])[0]
                del self._tasks[oldest]
            self._tasks[task_id] = {
                "task_id": task_id,
                "kind": kind,
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "result": None,
                "error": None,
            }

        def run() -> None:
            self._set(task_id, status="running")
            try:
                result = fn()
                self._set(task_id, status="done", result=result)
            except Exception as e:  # noqa: BLE001 - reported to the poller, never raised here
                self._set(
                    task_id,
                    status="failed",
                    error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}",
                )

        threading.Thread(target=run, daemon=True, name=f"task-{task_id}").start()
        return task_id

    def _set(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            self._tasks[task_id].update(fields)
            self._tasks[task_id]["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            return dict(self._tasks[task_id])
