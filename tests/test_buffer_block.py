from __future__ import annotations

import pytest

from logistics_planner.buffer_block import run_buffer_blocking
from logistics_planner.errors import FleetManagerError

from fakes import FakeFleet, FakeMesTcp, FakeRepository, load_test_config


def test_lock_state_is_mapped_by_bufno_not_array_index():
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[3, 5],
        lock_states={1: True, 2: True, 3: True, 5: False, 6: True},
        free_buffers={3, 5},
    )
    fleet = FakeFleet(
        tasks=[
            {
                "TaskStatus": "Started",
                "MESFromResource": 19,
                "MESFromBufNo": 5,
                "MESToResource": 1,
                "MESToBufNo": 1,
            }
        ]
    )
    mes = FakeMesTcp()

    run_buffer_blocking(repository, mes, fleet, config)

    assert fleet.calls == 1
    # 1 was actually booked and is no longer wanted; 3 was released; 5 is a
    # Robotino target and must stay booked.  With an array-index mapping the
    # commands would have been sent for buffers 2/4/6 instead.
    assert mes.buffer_calls == [(1, False), (3, False), (5, True)]


def test_release_limit_and_robotino_protection():
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[1, 3, 4, 5],
        lock_states={1: True, 2: True, 3: True, 4: True, 5: True, 6: True},
        free_buffers={1, 3, 4, 5},
    )
    fleet = FakeFleet(
        tasks=[
            {
                "TaskStatus": "Error",
                "MESFromResource": 19,
                "MESFromBufNo": 5,
                "MESToResource": 7,
                "MESToBufNo": 1,
            }
        ]
    )
    mes = FakeMesTcp()

    run_buffer_blocking(repository, mes, fleet, config)

    # Priorities [5,4,1,3]: 5 is Robotino-protected, 4 and 1 are released,
    # the limit of 2 is reached before 3.  Disabled 2/6 stay locked.
    assert mes.buffer_calls == [(1, False), (4, False)]


def test_fleet_manager_down_locks_safe_buffers_and_skips_unlocks():
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[1, 3],
        lock_states={1: True, 2: False, 3: False, 6: False},
        free_buffers={1},  # buffer 1 is a release candidate; 3 is occupied
    )
    fleet = FakeFleet(error=FleetManagerError("connection refused"))
    mes = FakeMesTcp()

    run_buffer_blocking(repository, mes, fleet, config)

    assert fleet.calls == 1
    # Disabled 2/6 are relocked even though the Fleet Manager is down, and the
    # occupied buffer 3 is locked.  Nothing is unlocked.
    assert mes.buffer_calls == [(2, True), (3, True), (6, True)]
    assert all(locked for _, locked in mes.buffer_calls)


def test_out_of_range_bufno_is_ignored_with_a_warning(caplog):
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[1, 7],
        lock_states={1: True, 7: False},
        free_buffers={1},
    )
    fleet = FakeFleet(tasks=[])
    mes = FakeMesTcp()

    with caplog.at_level("WARNING"):
        run_buffer_blocking(repository, mes, fleet, config)

    assert "BufNo 7" in caplog.text
    # Buffer 1 is still handled; the unknown 7 is never sent to MES.  The
    # disabled 2/6 are (correctly) locked on every cycle when MES reset them.
    assert (1, False) in mes.buffer_calls
    assert all(buffer_number != 7 for buffer_number, _ in mes.buffer_calls)


def test_fleet_task_status_finished_is_ignored():
    from logistics_planner.fleet import robotino_positions

    tasks = [
        {
            "TaskStatus": "Finished",
            "MESFromResource": 19,
            "MESFromBufNo": 1,
            "MESToResource": 19,
            "MESToBufNo": 2,
        },
        {
            "TaskStatus": "Started",
            "MESFromResource": 19,
            "MESFromBufNo": 3,
            "MESToResource": 19,
            "MESToBufNo": 4,
        },
    ]
    assert robotino_positions(tasks, 19) == [3, 4]


def test_steady_state_with_robotino_on_priority_buffer_sends_no_commands():
    """Regression: the lock pass must not lock a buffer the full pass releases.

    A Robotino is heading to BufNo 5 (highest priority), so the previous cycle
    released 4 and 1.  Without Robotino data the lock pass would have picked
    5 and 4 and locked 1, and the full pass would unlock 1 again: a 151/15 +
    151/16 pair every cycle with BufNo 1 briefly closed to MES.
    """
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[1, 3, 4, 5],
        lock_states={1: False, 2: True, 3: True, 4: False, 5: True, 6: True},
        free_buffers={1, 3, 4, 5},
    )
    fleet = FakeFleet(
        tasks=[
            {
                "TaskStatus": "Started",
                "MESFromResource": 7,
                "MESFromBufNo": 1,
                "MESToResource": 19,
                "MESToBufNo": 5,
            }
        ]
    )
    mes = FakeMesTcp(lock_states=repository.lock_states)

    run_buffer_blocking(repository, mes, fleet, config)

    assert mes.buffer_calls == []
    assert repository.lock_states == {1: False, 2: True, 3: True, 4: False, 5: True, 6: True}


def test_lock_pass_leaves_free_buffers_alone_when_fleet_manager_is_down():
    """Without Robotino data no free buffer is locked or unlocked."""
    config = load_test_config()
    repository = FakeRepository(
        config,
        positions_to_lock=[1, 3, 4, 5],
        lock_states={1: False, 2: False, 3: True, 4: False, 5: True, 6: True},
        free_buffers={1, 3, 4, 5},
    )
    fleet = FakeFleet(error=FleetManagerError("connection refused"))
    mes = FakeMesTcp(lock_states=repository.lock_states)

    run_buffer_blocking(repository, mes, fleet, config)

    # Only the disabled buffer 2 that an operator reset is relocked.
    assert mes.buffer_calls == [(2, True)]
