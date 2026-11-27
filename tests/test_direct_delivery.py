"""Direct delivery: box selection, store-order planning and mode policy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from logistics_planner.commissioning import create_commissioning_orders
from logistics_planner.direct_delivery import (
    plan_direct_delivery,
    select_direct_delivery,
)
from logistics_planner.errors import ConcurrentWriteError
from logistics_planner.queries import (
    ActiveCommissioningOrder,
    BoxSlot,
    DirectDemandGroup,
    PartDemand,
)

from fakes import FakeMesTcp, FakeRepository, load_test_config, resource_named


def _config(mode: str = "off", name: str = "RASS1"):
    config = load_test_config()
    return replace(
        config,
        resources=tuple(
            replace(resource, direct_delivery=mode) if resource.name == name else resource
            for resource in config.resources
        ),
    )


def _group(order_number: int, order_position: int, *part_numbers: int) -> DirectDemandGroup:
    return DirectDemandGroup(
        order_number=order_number,
        order_position=order_position,
        parts=tuple(
            PartDemand(order_number, order_position, 10, part_number)
            for part_number in part_numbers
        ),
    )


def _slot(box_id: int, box_position: int, part_number: int) -> BoxSlot:
    return BoxSlot(box_id=box_id, box_position=box_position, part_number=part_number)


def _demand(order_number: int = 100, part_number: int = 800) -> PartDemand:
    return PartDemand(order_number, 1, 10, part_number)


# ---------------------------------------------------------------------------
# selection (pure function)
# ---------------------------------------------------------------------------


def test_selection_requires_full_coverage():
    group = _group(100, 1, 800, 814)

    assert select_direct_delivery([group], {600: [_slot(600, 1, 800)]}, 8) is None

    selection = select_direct_delivery(
        [group],
        {600: [_slot(600, 1, 800), _slot(600, 2, 814)]},
        8,
    )
    assert selection == (
        600,
        [(group, [_slot(600, 1, 800), _slot(600, 2, 814)])],
    )


def test_selection_rejects_a_partial_box_and_uses_the_complete_one():
    group = _group(100, 1, 800, 814)
    boxes = {
        600: [_slot(600, 1, 800)],
        601: [_slot(601, 1, 800), _slot(601, 2, 814)],
    }

    box_id, assignments = select_direct_delivery([group], boxes, 8)

    assert box_id == 601
    assert assignments == [(group, [_slot(601, 1, 800), _slot(601, 2, 814)])]


def test_selection_counts_duplicate_parts():
    group = _group(100, 1, 800, 800)

    assert select_direct_delivery([group], {600: [_slot(600, 1, 800)]}, 8) is None

    box_id, assignments = select_direct_delivery(
        [group],
        {
            600: [_slot(600, 1, 800), _slot(600, 2, 800)],
            601: [
                _slot(601, 1, 800),
                _slot(601, 2, 800),
                _slot(601, 3, 800),
            ],
        },
        8,
    )

    assert box_id == 600
    assert assignments == [(group, [_slot(600, 1, 800), _slot(600, 2, 800)])]


def test_selection_bundles_several_positions_greedily_in_fifo_order():
    first = _group(100, 1, 800, 814)
    second = _group(101, 1, 800)
    third = _group(102, 1, 831)
    boxes = {
        600: [
            _slot(600, 1, 800),
            _slot(600, 2, 800),
            _slot(600, 3, 814),
        ]
    }

    box_id, assignments = select_direct_delivery([first, second, third], boxes, 8)

    assert box_id == 600
    assert assignments == [
        (first, [_slot(600, 1, 800), _slot(600, 3, 814)]),
        (second, [_slot(600, 2, 800)]),
    ]


def test_selection_uses_the_first_fitting_group_in_fifo_order():
    missing = _group(100, 1, 831)
    second = _group(101, 1, 800)
    third = _group(102, 1, 814)
    boxes = {600: [_slot(600, 1, 800)], 601: [_slot(601, 1, 814)]}

    box_id, assignments = select_direct_delivery([missing, second, third], boxes, 8)

    # The first group (831) fits no box; the next one (800) fits box 600, so
    # only boxes that fit that group are candidates -- even though box 601
    # could serve the later 814 group.
    assert box_id == 600
    assert assignments == [(second, [_slot(600, 1, 800)])]


def test_selection_tie_breaks_on_groups_then_parts_then_lowest_box():
    first = _group(100, 1, 800)
    second = _group(101, 1, 800, 814)
    third = _group(102, 1, 831)

    # Both boxes serve two groups; box 601 serves three parts, box 602 two.
    more_parts = {
        601: [_slot(601, 1, 800), _slot(601, 2, 800), _slot(601, 3, 814)],
        602: [_slot(602, 1, 800), _slot(602, 2, 831)],
    }
    assert select_direct_delivery([first, second, third], more_parts, 8)[0] == 601

    # Same group count and part count: the lowest box id wins.
    same = {
        604: [_slot(604, 1, 800), _slot(604, 2, 831)],
        603: [_slot(603, 1, 800), _slot(603, 2, 831)],
    }
    assert select_direct_delivery([first, second, third], same, 8)[0] == 603


def test_selection_never_exceeds_the_box_capacity():
    group = _group(100, 1, 800, 814, 831)
    box = {
        600: [
            _slot(600, 1, 800),
            _slot(600, 2, 814),
            _slot(600, 3, 831),
        ]
    }

    assert select_direct_delivery([group], box, 2) is None
    assert select_direct_delivery([group], box, 3) is not None


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_planning_creates_one_store_position_per_group_with_one_bundle_ono():
    config = _config("only")
    rass1 = resource_named(config, "RASS1")
    groups = [_group(100, 1, 800, 814), _group(101, 1, 800)]
    repository = FakeRepository(
        config,
        direct_demand_groups={"RASS1": groups},
        direct_boxes={
            600: [
                _slot(600, 1, 800),
                _slot(600, 2, 814),
                _slot(600, 3, 800),
            ]
        },
    )
    mes = FakeMesTcp()

    assert plan_direct_delivery(repository, mes, config, rass1) is True

    assert [call["workplan_disassembly_number"] for call in mes.disassembly_calls] == [6, 6]
    assert [call["order_number"] for call in mes.disassembly_calls] == [0, 900]
    assert [call["main_order_number"] for call in mes.disassembly_calls] == [100, 101]
    assert [call["main_order_position"] for call in mes.disassembly_calls] == [1, 1]
    assert (mes.disassembly_calls[0]["order"].order_number, mes.disassembly_calls[0]["order"].order_position) == (900, 1)
    assert (mes.disassembly_calls[1]["order"].order_number, mes.disassembly_calls[1]["order"].order_position) == (900, 2)
    assert mes.commissioning_calls == []
    assert repository.source_bookings == [
        (900, 1, 600, 1, 800),
        (900, 1, 600, 2, 814),
        (900, 2, 600, 3, 800),
    ]
    assert repository.commit_count == 2
    assert repository.rollback_count == 0


def test_planning_returns_false_without_a_covering_box():
    config = _config("only")
    rass1 = resource_named(config, "RASS1")
    repository = FakeRepository(
        config,
        direct_demand_groups={"RASS1": [_group(100, 1, 800, 814)]},
        direct_boxes={600: [_slot(600, 1, 800)]},
    )
    mes = FakeMesTcp()

    assert plan_direct_delivery(repository, mes, config, rass1) is False
    assert mes.disassembly_calls == []
    assert repository.source_bookings == []


# ---------------------------------------------------------------------------
# modes and guards through create_commissioning_orders
# ---------------------------------------------------------------------------


def test_prefer_falls_back_to_pick_and_sort_when_no_box_fits():
    config = _config("prefer")
    repository = FakeRepository(
        config,
        parts={"RASS1": [_demand()]},
        direct_demand_groups={"RASS1": [_group(100, 1, 800)]},
        direct_boxes={},
        new_box=12,
        boxes={12: list(range(1, 9))},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert len(mes.commissioning_calls) == 1
    assert mes.commissioning_calls[0]["box_id"] == 12
    assert mes.disassembly_calls == []
    assert repository.params7 == {(900, 1): 1}


def test_only_mode_waits_instead_of_creating_a_pick_and_sort_order(caplog):
    config = _config("only")
    repository = FakeRepository(
        config,
        parts={"RASS1": [_demand()]},
        direct_demand_groups={"RASS1": [_group(100, 1, 800)]},
        direct_boxes={},
        new_box=12,
        boxes={12: list(range(1, 9))},
    )
    mes = FakeMesTcp()

    with caplog.at_level("INFO"):
        create_commissioning_orders(repository, mes, config)

    assert mes.commissioning_calls == []
    assert mes.disassembly_calls == []
    assert repository.params7 == {}
    assert repository.boxes[12] == list(range(1, 9))
    assert "No ASRS20 box covers the demand" in caplog.text


def test_off_mode_keeps_the_pick_and_sort_behaviour():
    config = _config("off")
    repository = FakeRepository(
        config,
        parts={"RASS1": [_demand()]},
        direct_demand_groups={"RASS1": [_group(100, 1, 800)]},
        direct_boxes={600: [_slot(600, 1, 800)]},
        new_box=12,
        boxes={12: list(range(1, 9))},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert len(mes.commissioning_calls) == 1
    assert mes.commissioning_calls[0]["box_id"] == 12
    assert mes.disassembly_calls == []
    assert repository.source_bookings == []


def test_open_store_order_blocks_direct_and_pick_and_sort_planning():
    config = _config("prefer")
    repository = FakeRepository(
        config,
        parts={"RASS1": [_demand()]},
        direct_demand_groups={"RASS1": [_group(100, 1, 800)]},
        direct_boxes={600: [_slot(600, 1, 800)]},
        new_box=12,
        boxes={12: list(range(1, 9))},
        has_store_order={"RASS1": True},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert mes.disassembly_calls == []
    assert mes.commissioning_calls == []
    assert repository.params7 == {}
    assert repository.boxes[12] == list(range(1, 9))


def test_active_pick_and_sort_order_keeps_appending_and_does_not_mix():
    config = _config("prefer")
    repository = FakeRepository(
        config,
        parts={"RASS1": [_demand(100, 800), _demand(101, 814)]},
        direct_demand_groups={"RASS1": [_group(100, 1, 800), _group(101, 1, 814)]},
        direct_boxes={600: [_slot(600, 1, 800), _slot(600, 2, 814)]},
        active_orders={"RASS1": ActiveCommissioningOrder(order_number=500, box_id=11)},
        boxes={11: [1, 2]},
    )
    mes = FakeMesTcp()

    create_commissioning_orders(repository, mes, config)

    assert len(mes.commissioning_calls) == 2
    assert all(call["box_id"] == 11 for call in mes.commissioning_calls)
    assert mes.disassembly_calls == []
    assert repository.source_bookings == []


def test_failed_booking_after_mes_logs_the_sub_order_and_reraises(caplog):
    config = _config("only")
    rass1 = resource_named(config, "RASS1")
    repository = FakeRepository(
        config,
        direct_demand_groups={"RASS1": [_group(100, 1, 800, 814)]},
        direct_boxes={600: [_slot(600, 1, 800), _slot(600, 2, 814)]},
        fail_source_booking=True,
    )
    mes = FakeMesTcp()

    with caplog.at_level("ERROR"):
        with pytest.raises(ConcurrentWriteError, match="guarded source slot"):
            plan_direct_delivery(repository, mes, config, rass1)

    assert "Direct delivery" in caplog.text
    assert "900.1" in caplog.text
    assert "fixed by hand" in caplog.text
    assert len(mes.disassembly_calls) == 1
    assert repository.rollback_count == 1
    assert repository.source_bookings == []
