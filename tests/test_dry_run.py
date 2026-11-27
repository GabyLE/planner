from __future__ import annotations

import pytest

from logistics_planner.errors import QueryRowcountError
from logistics_planner.loop import PlannerRunner
from logistics_planner.queries import PartDemand

from fakes import FakeFleet, FakeMesTcp, FakeRepository, load_test_config, resource_named


def _repository(
    config,
    *,
    fail_target_position: bool = False,
    simulate: bool = True,
    read_only: bool = False,
) -> FakeRepository:
    return FakeRepository(
        config,
        simulate=simulate,
        read_only=read_only,
        parts={
            "RASS3": [
                PartDemand(1000, 1, 10, 831),
                PartDemand(1001, 1, 10, 832),
            ]
        },
        new_box=12,
        boxes={12: list(range(1, 9))},
        fail_target_position=fail_target_position,
    )


def test_simulate_cycle_rolls_back_all_planner_writes():
    config = load_test_config()
    repository = _repository(config)
    mes = FakeMesTcp()
    runner = PlannerRunner(config, repository, mes, FakeFleet())

    runner.run_plan_cycle()

    assert len(mes.commissioning_calls) == 2  # simulated TCP still happens
    assert repository.simulate_cycle_rolled_back is True
    assert repository.simulate_cycle_started is False
    # Every reservation/booking/parameter write was rolled back.
    assert repository.boxes[12] == list(range(1, 9))
    assert repository.params7 == {}
    assert repository.booked_slots == []
    assert repository.connected_checks == 1


def test_failed_simulate_cycle_is_still_rolled_back():
    config = load_test_config()
    repository = _repository(config, fail_target_position=True)
    mes = FakeMesTcp()
    runner = PlannerRunner(config, repository, mes, FakeFleet())

    with pytest.raises(QueryRowcountError, match="ParameterNo 7"):
        runner.run_plan_cycle()

    assert repository.simulate_cycle_rolled_back is True
    assert repository.simulate_cycle_started is False
    assert repository.boxes[12] == list(range(1, 9))
    assert repository.params7 == {}


def test_read_only_cycle_logs_but_changes_nothing():
    config = load_test_config()
    repository = _repository(config, simulate=False, read_only=True)
    mes = FakeMesTcp()
    runner = PlannerRunner(config, repository, mes, FakeFleet())

    runner.run_plan_cycle()

    # Policy still runs and the fake TCP records the intended commands.
    assert len(mes.commissioning_calls) == 2
    assert repository.boxes[12] == list(range(1, 9))
    assert repository.params7 == {}
    assert repository.booked_slots == []
    assert repository.simulate_cycle_started is False
    assert repository.simulate_cycle_rolled_back is False
    assert repository.connected_checks == 1


# --------------------------------------------------------------------------
# Real Repository / MesTcp in read-only dry-run mode.  The DB never changes,
# so without per-cycle shadow state every part would be "booked" into the
# same lowest free slot and synthetic ONo/OPos would count up every cycle.


class _DryRunDb:
    """Unchanging DB stub answering the few queries the dry-run paths use."""

    dry_run = True
    simulate = False
    in_transaction = False

    def __init__(self, free_slots, empty_boxes, used_sources=(), fullest_sources=(),
                 direct_demands=(), direct_slots=()):
        self.free_slots = free_slots  # box_id -> list of free BoxPos
        self.empty_boxes = empty_boxes  # ASRS boxes with all slots free, BufPos order
        self.used_sources = list(used_sources)
        self.fullest_sources = list(fullest_sources)
        self.direct_demands = list(direct_demands)
        self.direct_slots = list(direct_slots)
        self.writes = 0

    def query(self, sql, params=()):
        if 'Concat(tblstep.ONo, ".", tblstep.OPos) NOT IN' in sql:
            return list(self.direct_demands)
        if "booked.ONo <> 0" in sql:
            return list(self.direct_slots)
        if "Concat(tblstep.ONo" in sql:
            # The main Pick & Sort demand query.  Tests seed only RASS1
            # demands (feed op 323).
            return list(self.direct_demands) if 323 in params else []
        if "Count(tblorderpos.OPos) > 0 AS has" in sql:
            return [{"has": 0}]
        if "tblorderpos.WPNo = %s" in sql:
            return [{"has": 0}]
        if "Sum(tblbufferpos.PNo = 0) AS noBox" in sql:
            return [{"noBox": 1}]
        if "Count(*) AS freeSlots" in sql:
            return [{"freeSlots": len(self.free_slots.get(params[1], []))}]
        if "HAVING" in sql and "Count(tblboxpos.BoxPos) =" in sql:
            rows = [{"boxId": box_id} for box_id in self.empty_boxes]
            return rows if "LIMIT 1" not in sql else rows[:1]
        if "MostFullBox" in sql:
            return [{"boxId": b, "boxPosition": p} for b, p in self.fullest_sources][: params[-1]]
        if "sourceboxid.Parameter > 0" in sql:
            return [{"boxId": b, "boxPosition": p} for b, p in self.used_sources][: params[-1]]
        if "tblboxpos.BoxPos AS boxPosition" in sql or "tblboxpos.BoxPos as boxPosition" in sql:
            assert "FOR UPDATE" not in sql, "dry-run must not take row locks"
            return [{"boxPosition": pos} for pos in self.free_slots.get(params[1], [])]
        raise AssertionError(f"unexpected query: {sql}")

    def query_one(self, sql, params=()):
        if "MAX(ONo)" in sql:
            return {"maxOno": 9150}
        if "MAX(OPos)" in sql:
            return {"maxOpos": 3}
        raise AssertionError(f"unexpected query_one: {sql}")

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext(self)

    def execute(self, sql, params=()):
        self.writes += 1
        return 1


def _real_repository(db, config=None):
    from logistics_planner.queries import Repository

    return Repository(db, config or load_test_config())


def test_dry_run_books_each_part_into_a_distinct_target_slot():
    db = _DryRunDb(free_slots={500: [3, 5, 8]}, empty_boxes=[])
    repository = _real_repository(db)
    repository.begin_read_only_cycle()

    assert repository.count_free_box_slots(500) == 3
    assert [repository.reserve_free_box_slot(500) for _ in range(3)] == [3, 5, 8]
    assert repository.count_free_box_slots(500) == 0
    assert repository.box_has_empty_place(500) is False

    from logistics_planner.errors import CapacityError

    with pytest.raises(CapacityError):
        repository.reserve_free_box_slot(500)

    # The next cycle starts from the unchanged DB again.
    repository.begin_read_only_cycle()
    assert repository.count_free_box_slots(500) == 3
    assert repository.reserve_free_box_slot(500) == 3


def test_dry_run_does_not_pick_the_same_empty_box_twice_in_a_cycle():
    db = _DryRunDb(free_slots={12: list(range(1, 9)), 13: list(range(1, 9))}, empty_boxes=[12, 13])
    repository = _real_repository(db)
    repository.begin_read_only_cycle()

    assert repository.which_box_to_use_for_new_commissioning_orders(8) == 12
    repository.reserve_free_box_slot(12)
    # Box 12 is no longer empty in this cycle (e.g. for a disassembly box).
    assert repository.which_box_to_use_for_new_commissioning_orders(8) == 13


def test_dry_run_hands_out_each_source_slot_once_per_cycle():
    config = load_test_config()
    db = _DryRunDb(
        free_slots={},
        empty_boxes=[],
        used_sources=[(600, 1), (600, 2)],
        fullest_sources=[(601, 1), (601, 2)],
    )
    repository = _real_repository(db)
    repository.begin_read_only_cycle()
    rass3 = resource_named(config, "RASS3")

    first = repository.get_box_positions_for_parts_for_resource(rass3, 831, 2)
    assert [(p.box_id, p.box_position) for p in first] == [(600, 1), (600, 2)]
    for position in first:
        repository.book_order_to_box_position(9151, 1, position.box_id, position.box_position, 831)

    second = repository.get_box_positions_for_parts_for_resource(rass3, 831, 2)
    assert [(p.box_id, p.box_position) for p in second] == [(601, 1), (601, 2)]


def test_dry_run_synthetic_order_numbers_restart_every_cycle():
    from logistics_planner.config import Mes4Config
    from logistics_planner.mes_tcp import MesTcp

    repository = _real_repository(_DryRunDb(free_slots={}, empty_boxes=[]))
    mes = MesTcp(Mes4Config(host="127.0.0.1", port=1, timeout_s=1), read_only=True, repository=repository)

    def cycle():
        mes.begin_read_only_cycle()
        new = mes.create_commissioning_order(
            workplan_number=3, box_id=12, main_order_number=9000, main_order_position=1,
            main_step_number=160, part_number=831, order_number=0,
        )
        appended = mes.create_commissioning_order(
            workplan_number=3, box_id=12, main_order_number=9000, main_order_position=2,
            main_step_number=160, part_number=831, order_number=new.order_number,
        )
        existing = mes.create_commissioning_order(
            workplan_number=3, box_id=12, main_order_number=9000, main_order_position=3,
            main_step_number=160, part_number=831, order_number=7001,
        )
        return [(o.order_number, o.order_position) for o in (new, appended, existing)]

    assert cycle() == [(9151, 1), (9151, 2), (7001, 4)]
    assert cycle() == [(9151, 1), (9151, 2), (7001, 4)]


def test_read_only_runner_resets_shadow_state_each_cycle():
    config = load_test_config()
    repository = _repository(config, simulate=False, read_only=True)
    mes = FakeMesTcp()
    runner = PlannerRunner(config, repository, mes, FakeFleet())

    runner.run_plan_cycle()
    runner.run_plan_cycle()

    assert repository.read_only_cycles == 2
    assert mes.read_only_cycles == 2


def test_dry_run_direct_delivery_sends_no_tcp_and_books_each_slot_once(
    monkeypatch, caplog
):
    from dataclasses import replace

    from logistics_planner.commissioning import create_commissioning_orders
    from logistics_planner.config import Mes4Config
    from logistics_planner.direct_delivery import plan_direct_delivery
    from logistics_planner.mes_tcp import MesTcp

    config = load_test_config()
    config = replace(
        config,
        resources=tuple(
            replace(resource, direct_delivery="only")
            if resource.name == "RASS1"
            else resource
            for resource in config.resources
        ),
    )
    db = _DryRunDb(
        free_slots={},
        empty_boxes=[],
        direct_demands=[
            {"orderNumber": 9000, "orderPosition": 1, "stepNumber": 160, "partNumber": 800},
            {"orderNumber": 9000, "orderPosition": 1, "stepNumber": 170, "partNumber": 814},
        ],
        direct_slots=[
            {"boxId": 600, "boxPosition": 1, "partNumber": 800},
            {"boxId": 600, "boxPosition": 2, "partNumber": 814},
        ],
    )
    repository = _real_repository(db, config)
    mes = MesTcp(
        Mes4Config(host="127.0.0.1", port=1, timeout_s=1),
        read_only=True,
        repository=repository,
    )

    def no_tcp(*args, **kwargs):
        raise AssertionError("read-only direct delivery opened a TCP connection")

    monkeypatch.setattr("socket.create_connection", no_tcp)
    repository.begin_read_only_cycle()
    mes.begin_read_only_cycle()

    with caplog.at_level("INFO"):
        create_commissioning_orders(repository, mes, config)

    assert "[dry-run] would send TCP" in caplog.text
    assert "direct delivery" in caplog.text
    assert repository._shadow_source_slots == {(600, 1), (600, 2)}

    # A second direct plan in the same cycle cannot see the box as unbooked
    # any more, so no slot is handed out twice.
    rass1 = resource_named(config, "RASS1")
    assert repository.fully_unbooked_asrs_boxes_with_parts([800, 814]) == {}
    assert plan_direct_delivery(repository, mes, config, rass1) is False
