from __future__ import annotations

import pytest

from logistics_planner.commissioning import (
    create_commissioning_orders,
    reconcile_unbooked_positions,
)
from logistics_planner.errors import MesTcpError, PlanningError, QueryRowcountError
from logistics_planner.queries import (
    ActiveCommissioningOrder,
    PartDemand,
    UnbookedPosition,
)

from fakes import FakeMesTcp, FakeRepository, load_test_config, resource_named


def _demands(count: int, start_part: int = 831) -> list[PartDemand]:
    return [
        PartDemand(order_number=1000 + index, order_position=1, step_number=10, part_number=start_part + index)
        for index in range(count)
    ]


def test_reported_scenario_existing_box_capacity_limits_created_orders():
    config = load_test_config()
    rass3 = resource_named(config, "RASS3")
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(5)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [1, 2, 3]},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    # F = 3 free slots, N = 5 requested -> exactly 3 commissioning positions.
    assert len(mes.commissioning_calls) == 3
    assert all(call["box_id"] == 11 for call in mes.commissioning_calls)
    assert [call["order"].order_number for call in mes.commissioning_calls] == [500, 500, 500]
    assert [call["order"].order_position for call in mes.commissioning_calls] == [1, 2, 3]
    assert sorted(repository.params7.values()) == [1, 2, 3]
    assert all(position >= 1 for position in repository.params7.values())
    assert repository.boxes[11] == []


def test_remaining_parts_are_planned_next_cycle_into_new_box():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(5)[3:]},
        new_box=12,
        boxes={12: list(range(1, 9))},
    )
    mes = FakeMesTcp(next_order_number=901)

    create_commissioning_orders(repository, mes, config)

    assert len(mes.commissioning_calls) == 2
    assert all(call["box_id"] == 12 for call in mes.commissioning_calls)
    assert [call["order"].order_number for call in mes.commissioning_calls] == [901, 901]
    assert sorted(repository.params7.values()) == [1, 2]


def test_ambiguous_active_box_skips_resource_without_creating_orders():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(1)},
        has_commissioning_order={"RASS3": True},
        active_errors={"RASS3": PlanningError("active in boxes 11, 12")},
    )
    mes = FakeMesTcp()

    # The repository raises PlanningError; the cycle logs it and skips the
    # resource instead of guessing a box or aborting the whole process.
    create_commissioning_orders(repository, mes, config)
    assert mes.commissioning_calls == []
    assert repository.params7 == {}


def test_parameter_7_rowcount_failure_rolls_back_and_aborts():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(2)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [4, 5]},
        fail_target_position=True,
    )
    mes = FakeMesTcp()

    with pytest.raises(QueryRowcountError, match="ParameterNo 7"):
        create_commissioning_orders(repository, mes, config)

    # The reservation is rolled back; exactly one MES position was created
    # before the failing write (MES commits its own rows and cannot be undone).
    assert repository.boxes[11] == [4, 5]
    assert repository.params7 == {}
    assert repository.booked_slots == []
    assert len(mes.commissioning_calls) == 1
    assert repository.rollback_count == 1


def test_commissioning_tcp_failure_touches_no_db_transaction():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(1)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [7, 8]},
    )
    mes = FakeMesTcp(fail_commissioning=True)

    with pytest.raises(MesTcpError, match="commissioning TCP"):
        create_commissioning_orders(repository, mes, config)

    # 101/6 is sent before the DB transaction starts, so there is nothing to
    # roll back and the slot was never reserved.
    assert repository.boxes[11] == [7, 8]
    assert repository.params7 == {}
    assert repository.booked_slots == []
    assert repository.rollback_count == 0
    assert repository.commit_count == 0


def test_guarded_slot_update_failure_rolls_back_and_aborts():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(2)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [1, 2]},
        fail_slot_booking=True,
    )
    mes = FakeMesTcp()

    with pytest.raises(QueryRowcountError, match="guarded slot"):
        create_commissioning_orders(repository, mes, config)

    assert repository.boxes[11] == [1, 2]
    assert repository.params7 == {}
    assert repository.rollback_count == 1


def test_enabled_order_without_open_position_gets_a_new_order():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(1)},
        has_commissioning_order={"RASS3": True},
        new_box=12,
        boxes={12: list(range(1, 9))},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    # There is no End IS NULL position, so there is no active order to append
    # to; a fresh target box and order are planned.
    assert len(mes.commissioning_calls) == 1
    assert mes.commissioning_calls[0]["box_id"] == 12
    assert repository.params7 == {(900, 1): 1}


def test_reconcile_books_free_slot_and_writes_parameter_7():
    config = load_test_config()
    repository = FakeRepository(
        config,
        boxes={11: [4, 5]},
        unbooked=[
            UnbookedPosition(
                order_number=900,
                order_position=1,
                box_id=11,
                target_position="0",
                existing_box_position=None,
            )
        ],
    )

    reconcile_unbooked_positions(repository, config)

    assert repository.params7 == {(900, 1): 4}
    assert repository.booked_slots == [(11, 4, 900, 1)]
    assert repository.boxes[11] == [5]


def test_reconcile_reuses_existing_slot_without_double_booking():
    config = load_test_config()
    repository = FakeRepository(
        config,
        boxes={11: [4, 5]},
        unbooked=[
            UnbookedPosition(
                order_number=900,
                order_position=1,
                box_id=11,
                target_position="0",
                existing_box_position=7,
            )
        ],
    )

    reconcile_unbooked_positions(repository, config)

    assert repository.params7 == {(900, 1): 7}
    assert repository.booked_slots == []
    assert repository.boxes[11] == [4, 5]


def test_reconcile_full_box_logs_error_and_writes_nothing(caplog):
    config = load_test_config()
    repository = FakeRepository(
        config,
        boxes={11: []},
        unbooked=[
            UnbookedPosition(
                order_number=900,
                order_position=1,
                box_id=11,
                target_position="0",
                existing_box_position=None,
            )
        ],
    )

    with caplog.at_level("ERROR"):
        reconcile_unbooked_positions(repository, config)

    assert "full" in caplog.text
    assert "900.1" in caplog.text
    assert repository.params7 == {}
    assert repository.booked_slots == []


def test_active_box_in_disabled_buffer_skips_resource():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(1)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [1, 2]},
        disabled_boxes={11},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert mes.commissioning_calls == []
    assert repository.params7 == {}


def test_param7_failure_is_repaired_by_next_reconcile():
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS3": _demands(1)},
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [4, 5]},
        fail_target_position=True,
    )
    mes = FakeMesTcp()

    with pytest.raises(QueryRowcountError, match="ParameterNo 7"):
        create_commissioning_orders(repository, mes, config)

    # MES created 900.1 before the param-7 write failed; the local booking
    # rolled back, so next cycle sees exactly this orphan.
    assert len(mes.commissioning_calls) == 1
    assert mes.commissioning_calls[0]["order"].order_number == 500
    repository.fail_target_position = False
    repository.unbooked = [
        UnbookedPosition(
            order_number=500,
            order_position=1,
            box_id=11,
            target_position="0",
            existing_box_position=None,
        )
    ]

    reconcile_unbooked_positions(repository, config)

    assert repository.params7 == {(500, 1): 4}
    assert repository.booked_slots == [(11, 4, 500, 1)]


def test_stuck_box_in_disabled_buffer_does_not_start_a_second_order():
    """A commissioning box stuck in a disabled buffer stops planning.

    RASS1's active order is in box 11, which sits in a disabled Pick&Sort
    buffer.  RASS2 has open demands and a free empty box is available, but
    planning it would run two commissioning orders at once.
    """
    config = load_test_config()
    repository = FakeRepository(
        config,
        parts={"RASS1": _demands(1), "RASS2": _demands(2, start_part=841)},
        active_orders={"RASS1": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [1, 2], 12: list(range(1, 9))},
        new_box=12,
        disabled_boxes={11},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert mes.commissioning_calls == []
    assert repository.booked_slots == []
    assert repository.boxes[12] == list(range(1, 9))


def test_unfixable_orphan_is_reported_once_across_cycles(caplog):
    """A full-box orphan logs one ERROR, not one every cycle."""
    config = load_test_config()
    orphan = UnbookedPosition(
        order_number=900,
        order_position=1,
        box_id=11,
        target_position="0",
        existing_box_position=None,
    )
    repository = FakeRepository(config, boxes={11: []}, unbooked=[orphan])
    reported: set[tuple[int, int]] = set()

    with caplog.at_level("DEBUG"):
        for _ in range(3):
            reconcile_unbooked_positions(repository, config, reported)

    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    assert "900.1" in errors[0].getMessage()
    assert "full" in errors[0].getMessage()
    assert reported == {(900, 1)}
    assert repository.params7 == {}

    # Once it is fixed by hand it disappears and is forgotten ...
    repository.unbooked = []
    reconcile_unbooked_positions(repository, config, reported)
    assert reported == set()

    # ... so a recurrence is reported again.
    repository.unbooked = [orphan]
    caplog.clear()
    with caplog.at_level("ERROR"):
        reconcile_unbooked_positions(repository, config, reported)
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_runner_keeps_orphan_reports_across_cycles(caplog):
    from logistics_planner.loop import PlannerRunner

    from fakes import FakeFleet

    config = load_test_config()
    repository = FakeRepository(
        config,
        boxes={11: []},
        unbooked=[
            UnbookedPosition(
                order_number=900,
                order_position=1,
                box_id=11,
                target_position="0",
                existing_box_position=None,
            )
        ],
    )
    runner = PlannerRunner(config, repository, FakeMesTcp(), FakeFleet())

    with caplog.at_level("ERROR"):
        runner.run_plan_cycle()
        runner.run_plan_cycle()

    assert len([r for r in caplog.records if "900.1" in r.getMessage()]) == 1
