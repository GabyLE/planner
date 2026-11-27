"""Fleet Manager client (TaskHistory).

The Fleet Manager is the source of truth for Robotino source/target buffer
positions.  A failure here must abort the buffer-blocking cycle *before* any
lock/unlock command is sent, so nothing is unlocked by accident.

Reference: ``.../src/utils/fleetManager.ts``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from .config import FleetManagerConfig
from .errors import FleetManagerError

_ACTIVE_STATUSES = ("Started", "Error")


def robotino_positions(tasks: Iterable[Any], resource_id: int) -> list[int]:
    """Return the BufferNo values of active Robotino tasks at ``resource_id``.

    Tasks are matched on ``MESFromResource``/``MESToResource`` and the buffer
    number is taken from the matching ``MESFromBufNo``/``MESToBufNo``.  The
    result is de-duplicated but keeps task order.
    """
    positions: dict[int, None] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise FleetManagerError(f"fleet task {index} is not an object")
        if task.get("TaskStatus") not in _ACTIVE_STATUSES:
            continue
        for resource_key, buffer_key in (
            ("MESFromResource", "MESFromBufNo"),
            ("MESToResource", "MESToBufNo"),
        ):
            if task.get(resource_key) != resource_id:
                continue
            buffer_number = task.get(buffer_key)
            if isinstance(buffer_number, bool) or not isinstance(buffer_number, int):
                raise FleetManagerError(
                    f"fleet task {index} has invalid {buffer_key}={buffer_number!r}"
                )
            positions.setdefault(buffer_number, None)
    return list(positions)


class FleetManager:
    def __init__(self, config: FleetManagerConfig) -> None:
        self._config = config

    @property
    def history_url(self) -> str:
        return self._config.host.rstrip("/") + "/GetTaskHistory"

    def get_task_history(self) -> list[dict[str, Any]]:
        url = self.history_url
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_s) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FleetManagerError(f"fleet manager {url} unreachable: {exc}") from exc
        if status >= 400:  # pragma: no cover - urlopen already raises HTTPError
            raise FleetManagerError(f"fleet manager {url} returned HTTP {status}")
        try:
            tasks = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FleetManagerError(f"fleet manager {url} returned invalid JSON: {exc}") from exc
        if not isinstance(tasks, list):
            raise FleetManagerError(f"fleet manager {url} returned {type(tasks).__name__}, not an array")
        return tasks

    def get_robotino_positions(self, resource_id: int) -> list[int]:
        return robotino_positions(self.get_task_history(), resource_id)
