from __future__ import annotations

import pytest

from logistics_planner.commissioning import book_commissioning_orders
from logistics_planner.errors import QueryRowcountError
from logistics_planner.queries import (
    ActiveCommissioningOrder,
    BoxPosition,
    OrderRef,
    PartRequest,
)

from fakes import FakeMesTcp, FakeRepository, load_test_config


def test_missing_part_is_skipped_and_next_part_is_booked():
    config = load_test_config()
    repository = FakeRepository(
        config,
        part_requests={
            "RASS3": [
                PartRequest(part_number=800, quantity=1, orders=[OrderRef(100, 1)]),
                PartRequest(part_number=811, quantity=1, orders=[OrderRef(100, 2)]),
            ]
        },
        box_positions={
            ("RASS3", 800): [],
            ("RASS3", 811): [BoxPosition(box_id=30, box_position=7)],
        },
    )
    mes = FakeMesTcp()

    book_commissioning_orders(repository, mes, config)

    assert repository.source_bookings == [(100, 2, 30, 7, 811)]
    assert repository.source_box_ids == [(100, 2, 30)]
    assert repository.source_box_positions == [(100, 2, 7)]
    assert repository.commit_count == 1
    assert repository.rollback_count == 0


def test_source_slot_rowcount_failure_rolls_back_order_booking():
    config = load_test_config()
    repository = FakeRepository(
        config,
        part_requests={
            "RASS3": [PartRequest(part_number=800, quantity=1, orders=[OrderRef(100, 1)])]
        },
        box_positions={("RASS3", 800): [BoxPosition(box_id=30, box_position=1)]},
        fail_source_booking=True,
    )
    mes = FakeMesTcp()

    with pytest.raises(QueryRowcountError, match="source slot"):
        book_commissioning_orders(repository, mes, config)

    assert repository.source_bookings == []
    assert repository.source_box_ids == []
    assert repository.rollback_count == 1


def test_parameter_3_failure_rolls_back_source_slot_booking():
    config = load_test_config()
    repository = FakeRepository(
        config,
        part_requests={
            "RASS3": [PartRequest(part_number=800, quantity=1, orders=[OrderRef(100, 1)])]
        },
        box_positions={("RASS3", 800): [BoxPosition(box_id=30, box_position=1)]},
        fail_source_box_id=True,
    )
    mes = FakeMesTcp()

    with pytest.raises(QueryRowcountError, match="ParameterNo 3"):
        book_commissioning_orders(repository, mes, config)

    # The source-slot booking and parameter writes share one transaction.
    assert repository.source_bookings == []
    assert repository.source_box_ids == []
    assert repository.rollback_count == 1


def test_source_booking_is_skipped_for_disabled_target_box():
    config = load_test_config()
    repository = FakeRepository(
        config,
        active_orders={"RASS3": ActiveCommissioningOrder(order_number=500, box_id=11)},
        disabled_boxes={11},
        part_requests={
            "RASS3": [PartRequest(part_number=800, quantity=1, orders=[OrderRef(100, 1)])]
        },
        box_positions={("RASS3", 800): [BoxPosition(box_id=30, box_position=7)]},
    )
    mes = FakeMesTcp()

    book_commissioning_orders(repository, mes, config)

    assert repository.source_bookings == []
    assert repository.source_box_ids == []
    assert repository.source_box_positions == []
