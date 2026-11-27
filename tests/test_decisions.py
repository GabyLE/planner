from __future__ import annotations

import dataclasses
import json

from logistics_planner.commissioning import book_commissioning_orders
from logistics_planner.decisions import (
    JsonlDecisionLog,
    RulePolicy,
    SourceSlotCandidate,
    SourceSlotContext,
    after_source_pick,
)
from logistics_planner.queries import BoxPosition, OrderRef, PartRequest

from fakes import FakeMesTcp, FakeRepository, load_test_config

CONTEXT = SourceSlotContext("RASS3", 3, 831, 100, 1, 3, 0)


def _slot(box: int, pos: int, *, used: bool = False, free: int = 1) -> SourceSlotCandidate:
    return SourceSlotCandidate(
        box_id=box, box_position=pos, part_number=831, used_as_source=used,
        asrs_buf_pos=1, free_of_part_in_box=free, empty_slots_in_box=0, part_slots_in_box=free,
    )


def test_rule_prefers_used_box_then_fullest_then_lowest_id():
    policy = RulePolicy()
    fullest = [_slot(20, 3, free=5), _slot(10, 1, free=2), _slot(30, 1, free=5)]
    assert fullest[policy.choose_source_slot(CONTEXT, fullest)] == fullest[0]

    used = fullest + [_slot(40, 2, used=True), _slot(40, 1, used=True)]
    assert used[policy.choose_source_slot(CONTEXT, used)] == _slot(40, 1, used=True)


def test_after_pick_removes_slot_and_marks_box_as_source():
    candidates = [_slot(20, 1, free=2), _slot(20, 2, free=2), _slot(30, 1, free=3)]
    remaining = after_source_pick(candidates, candidates[0])
    # Booking does not change slot contents, only availability.
    picked_box = dataclasses.replace(_slot(20, 2, used=True, free=1), part_slots_in_box=2)
    assert remaining == [picked_box, _slot(30, 1, free=3)]
    # The box just picked from stays preferred, as in the SQL's single query.
    assert remaining[RulePolicy().choose_source_slot(CONTEXT, remaining)].box_id == 20


class _Recorder:
    def __init__(self) -> None:
        self.records: list[tuple] = []

    def begin_cycle(self) -> None:
        pass

    def decision(self, kind, policy, context, candidates, chosen) -> None:
        self.records.append((kind, policy, context, list(candidates), chosen))


def test_booking_records_each_pick_and_uses_the_policy_choice():
    config = load_test_config()
    repository = FakeRepository(
        config,
        part_requests={"RASS3": [
            PartRequest(part_number=811, quantity=2, orders=[OrderRef(100, 1), OrderRef(100, 2)]),
            PartRequest(part_number=812, quantity=1, orders=[OrderRef(100, 3)]),
        ]},
        box_positions={
            ("RASS3", 811): [BoxPosition(30, 1), BoxPosition(30, 2), BoxPosition(31, 1)],
            ("RASS3", 812): [],
        },
    )

    class LastSlot:
        name = "last-slot"

        def choose_source_slot(self, context, candidates):
            return len(candidates) - 1

    recorder = _Recorder()
    book_commissioning_orders(repository, FakeMesTcp(), config, policy=LastSlot(),
                              decisions=recorder)

    assert repository.source_bookings == [(100, 1, 31, 1, 811), (100, 2, 30, 2, 811)]
    kinds = [(r[0], r[1], r[2].pick, len(r[3]), r[4]) for r in recorder.records]
    assert kinds == [
        ("source_slot", "last-slot", 0, 3, 2),
        ("source_slot", "last-slot", 1, 2, 1),
        ("source_slot", "last-slot", 0, 0, None),  # part 812: nothing to choose from
    ]


def test_jsonl_log_writes_context_candidates_and_choice(tmp_path):
    path = tmp_path / "decisions.jsonl"
    log = JsonlDecisionLog(path, mode="dry-run")
    log.begin_cycle()
    log.decision("source_slot", "rule-v1", CONTEXT, [_slot(20, 1), _slot(20, 2)], 1)

    (record,) = [json.loads(line) for line in path.read_text().splitlines()]
    assert record["kind"] == "source_slot" and record["mode"] == "dry-run"
    assert record["cycle"] == 0 and record["seq"] == 0 and record["chosen"] == 1
    assert record["context"]["part_number"] == 831
    assert [c["box_position"] for c in record["candidates"]] == [1, 2]
