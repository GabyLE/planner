from __future__ import annotations

import pytest

from logistics_planner.disassembly import book_disassembly_orders
from logistics_planner.errors import MesTcpError
from logistics_planner.queries import ActiveCommissioningOrder, DisassemblyRequest

from fakes import FakeMesTcp, FakeRepository, load_test_config


def _requests(*pairs: tuple[int, int]) -> list[DisassemblyRequest]:
    return [DisassemblyRequest(order_number=order, order_position=position) for order, position in pairs]


def test_commissioning_active_serves_only_aass_resources():
    config = load_test_config()
    repository = FakeRepository(
        config,
        active_orders={"RASS1": ActiveCommissioningOrder(order_number=500, box_id=11)},
        disassembly_requests_without_store={
            "CP-F-AASS1": _requests((1000, 1), (1000, 2), (1000, 3))
        },
        disassembly_boxes={"CP-F-AASS1": 20},
        boxes={20: [1, 2]},
    )
    mes = FakeMesTcp()

    book_disassembly_orders(repository, mes, config)

    # Only AASS (WPdis 0 + remove op 620) is served while commissioning is
    # active.  MANDOCK also has WPdis 0 but uses manual-boxing op 502.
    assert "PALROB" not in repository.disassembly_queries
    assert "RASS1" not in repository.disassembly_queries
    assert "RASS2" not in repository.disassembly_queries
    assert "RASS3" not in repository.disassembly_queries
    assert "MANDOCK" not in repository.disassembly_queries
    assert "CP-F-AASS1" in repository.disassembly_queries

    # Box 20 had two free slots, so only two of the three requests were booked.
    assert repository.booked_slots == [
        (20, 1, 1000, 1),
        (20, 2, 1000, 2),
    ]
    assert repository.boxes[20] == []


def test_tcp_disassembly_creation_is_capacity_limited_and_reuses_ono():
    config = load_test_config()
    repository = FakeRepository(
        config,
        disassembly_requests={"PALROB": _requests((2000, 1), (2000, 2), (2000, 3))},
        new_box=30,
        boxes={30: [1, 2]},
    )
    mes = FakeMesTcp(next_order_number=900)

    book_disassembly_orders(repository, mes, config)

    assert len(mes.disassembly_calls) == 2
    assert [call["workplan_disassembly_number"] for call in mes.disassembly_calls] == [9, 9]
    assert [call["order_number"] for call in mes.disassembly_calls] == [0, 900]
    assert [call["order"].order_number for call in mes.disassembly_calls] == [900, 900]
    assert repository.booked_slots == [
        (30, 1, 900, 1),
        (30, 2, 900, 2),
    ]


def test_disassembly_tcp_failure_touches_no_db_transaction():
    config = load_test_config()
    repository = FakeRepository(
        config,
        disassembly_requests={"PALROB": _requests((2000, 1))},
        new_box=30,
        boxes={30: [1, 2]},
    )
    mes = FakeMesTcp(fail_disassembly=True)

    with pytest.raises(MesTcpError, match="disassembly TCP"):
        book_disassembly_orders(repository, mes, config)

    # The 101/6 call is made before the DB transaction, so no slot was
    # reserved and there is nothing to roll back.
    assert repository.boxes[30] == [1, 2]
    assert repository.booked_slots == []
    assert repository.rollback_count == 0
    assert repository.commit_count == 0


def test_active_disassembly_order_blocks_new_planning():
    config = load_test_config()
    repository = FakeRepository(
        config,
        has_disassembly_order={"CP-F-AASS1": True},
        disassembly_requests_without_store={"CP-F-AASS1": _requests((1000, 1))},
        boxes={20: [1]},
    )
    mes = FakeMesTcp()

    book_disassembly_orders(repository, mes, config)

    assert repository.booked_slots == []
    assert mes.disassembly_calls == []
