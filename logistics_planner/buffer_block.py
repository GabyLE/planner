"""Pick & Sort buffer locking.

Ported from ``buffer-blocking.ts`` with the following fixes:

* locking is split from unlocking.  Disabled, occupied and empty-by-definition
  buffers are locked *before* the Fleet Manager is queried; only the unlock
  pass needs Fleet Manager task data.  If the Fleet Manager is unreachable the
  cycle still relocks the broken/occupied buffers and skips every unlock;
* lock states are mapped by ``BufNo`` instead of by array index;
* unknown buffer numbers are logged and ignored instead of aborting the whole
  cycle;
* each reply's ``ErrorState`` is checked;
* disabled buffers stay locked and Robotino source/target buffers are never
  released.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .config import Config, PickAndSortConfig
from .errors import FleetManagerError
from .fleet import FleetManager, robotino_positions
from .mes_tcp import MesTcp
from .queries import Repository

log = logging.getLogger(__name__)


def _priority_of(position: int, priorities: tuple[int, ...]) -> int:
    try:
        return priorities.index(position)
    except ValueError:
        return len(priorities)


def _known_buffers(
    values: Iterable[int],
    settings: PickAndSortConfig,
    source: str,
) -> set[int]:
    """Drop out-of-range BufNo values with a warning instead of aborting."""
    known: set[int] = set()
    for value in values:
        if 1 <= value <= settings.number_of_buffers:
            known.add(value)
        else:
            log.warning(
                "%s contains BufNo %s outside the configured range 1..%s; ignoring it",
                source,
                value,
                settings.number_of_buffers,
            )
    return known


def _known_lock_states(
    states: dict[int, bool],
    settings: PickAndSortConfig,
) -> dict[int, bool]:
    return {
        buffer_number: bool(states[buffer_number])
        for buffer_number in _known_buffers(states, settings, "actual buffer lock state")
    }


def _lock_pass_state(
    repository: Repository,
    positions_to_lock: set[int],
    settings: PickAndSortConfig,
) -> dict[int, bool]:
    """Buffers that must be locked regardless of the Fleet Manager.

    Disabled buffers plus every lock candidate that is not free (holds a box).
    Free buffers are absent from the result, so the lock-only pass never
    touches them.
    """
    desired = {position: True for position in settings.disabled_buffers}
    for position in positions_to_lock:
        if position in desired:
            continue
        if not repository.pick_sort_position_is_free(position):
            desired[position] = True
    return desired


def _desired_state(
    repository: Repository,
    positions_to_lock: set[int],
    robotino: set[int],
    settings: PickAndSortConfig,
) -> dict[int, bool]:
    """Full desired lock state, including the up-to-N release decisions."""
    desired = {
        buffer_number: False
        for buffer_number in range(1, settings.number_of_buffers + 1)
    }
    for position in positions_to_lock:
        desired[position] = True
    for position in robotino:
        desired[position] = True
    for position in settings.disabled_buffers:
        desired[position] = True

    ordered = sorted(
        positions_to_lock,
        key=lambda position: _priority_of(position, settings.buffer_pos_priorities),
    )
    log.info("Buffer positions to check for free place: %s", ordered)
    released = 0
    for position in ordered:
        if released >= settings.number_of_free_buffers:
            break
        if position in settings.disabled_buffers or position in robotino:
            continue
        if repository.pick_sort_position_is_free(position):
            desired[position] = False
            released += 1
    return desired


def _apply(
    mes: MesTcp,
    config: Config,
    desired: dict[int, bool],
    actual: dict[int, bool],
    *,
    lock_only: bool,
) -> None:
    for buffer_number in sorted(set(desired) | set(actual)):
        new_locking = desired.get(buffer_number, False)
        actual_locking = actual.get(buffer_number, False)
        if new_locking == actual_locking:
            continue
        if lock_only and not new_locking:
            # The lock pass is deliberately incapable of unlocking anything.
            continue
        mes.set_buffer_booked(
            resource_id=config.pick_and_sort_resource_id,
            buffer_number=buffer_number,
            locked=new_locking,
        )
        log.info(
            "Buffer %s %s", buffer_number, "locked" if new_locking else "unlocked"
        )


def run_buffer_blocking(
    repository: Repository,
    mes: MesTcp,
    fleet: FleetManager,
    config: Config,
) -> None:
    """One buffer-blocking cycle (buffer-blocking.ts:15-85)."""
    settings = config.pick_and_sort
    log.info("")
    log.info("")
    log.info("#########################################################")
    log.info("# New booking cycle started                             #")
    log.info("#########################################################")
    log.info("")

    positions_to_lock = _known_buffers(
        repository.pick_sort_positions_to_lock(),
        settings,
        "positions to lock",
    )
    actual_lockings = _known_lock_states(
        repository.pick_sort_lock_states(), settings
    )
    log.info("Buffer positions to lock: %s", sorted(positions_to_lock))
    log.info("Actual buffer lock state by BufNo: %s", actual_lockings)

    # Lock pass: disabled buffers and buffers that hold a box are made safe
    # without consulting the Fleet Manager.  This also repairs buffers that
    # MES or an operator reset to Booked=0.  Free (release-candidate) buffers
    # are left to the full pass: choosing which of them to release needs the
    # Robotino positions, and deciding it here without them would lock a
    # buffer that the full pass unlocks again in the same cycle.
    lock_desired = _lock_pass_state(repository, positions_to_lock, settings)
    _apply(mes, config, lock_desired, actual_lockings, lock_only=True)

    try:
        tasks = fleet.get_task_history()
        robotino = _known_buffers(
            robotino_positions(tasks, config.pick_and_sort_resource_id),
            settings,
            "Fleet Manager task history",
        )
    except FleetManagerError as exc:
        log.error(
            "Fleet Manager unavailable; locked the safe buffers and skipped all "
            "unlocks for this cycle: %s",
            exc,
        )
        return
    actual_lockings = _known_lock_states(
        repository.pick_sort_lock_states(), settings
    )
    log.info("Robotino source or target positions: %s", sorted(robotino))

    desired = _desired_state(repository, positions_to_lock, robotino, settings)
    log.info("Buffer positions lock state: %s", desired)
    _apply(mes, config, desired, actual_lockings, lock_only=False)


# Kept for callers that prefer the old module-level name.
run = run_buffer_blocking


__all__ = ["run_buffer_blocking", "run"]
