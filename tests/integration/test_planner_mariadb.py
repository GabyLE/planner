"""MariaDB integration tests for the Python logistics planner.

These tests use the real planner CLI, a scratch MariaDB loaded from the July
2026 MES4 dump and the fake MES4/Fleet Manager services from ``conftest.py``.
They intentionally assert *invariants over the resulting MES rows* rather than
a byte-for-byte copy of one historical planning cycle: the dump is a single
point in time and the planner is allowed to choose a different but valid box
or slot while keeping the published contract.

The central regression is the TypeScript over-planning bug: when a target box
has fewer free slots than open demands, the old loop still created one
commissioning position per demand and wrote `ParameterNo 7 = 0` for the
positions it could not book.  A zero target position makes MES4's op-end SQL
update nothing, so the part silently disappears from the source box.  The new
planner must cap the number of demands by the box capacity and must never
create a commissioning position with a non-positive target position.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

# Keep these in sync with conftest.py and the planner config contract.
BOX_TYPE_PNO = 29
BOX_CAPACITY = 8
ASRS_RESOURCE_ID = 17
PICK_AND_SORT_RESOURCE_ID = 19
RASS3_RESOURCE_ID = 6
RASS3_WORKPLAN = 3
RASS3_FEED_OP = 325
RASS1_RESOURCE_ID = 2
RASS1_WORKPLAN = 1
RASS1_STORE_WORKPLAN = 6
RASS1_FEED_OP = 323
PART_NO = 831

MAIN_ORDER = 9000
MAIN_WP = 1000
MAIN_STEP = 160
DEMAND_COUNT = 5
ACTIVE_COMMISSIONING_ORDER = 7001
ACTIVE_COMMISSIONING_LINK = (8999, 1, MAIN_STEP)


def _seed_main_demands(db: Any, count: int = DEMAND_COUNT) -> set[tuple[int, int, int]]:
    """Five open RASS3 feed steps in one open main order.

    The main order uses the real workplan 1000; the demand query selects its
    op-325 steps and reads the part number from the step parameter whose
    ``tbloperationparameter.ValueType`` is 3 (ParameterNo 3 for op 325).
    """
    start = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) "
        "VALUES (%s, %s, 0, 0, 1)",
        (MAIN_ORDER, start),
    )

    demands: set[tuple[int, int, int]] = set()
    for index in range(1, count + 1):
        planned = start + timedelta(seconds=index)
        db.execute(
            "INSERT INTO tblorderpos "
            "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
            " OpNo, PNo, OrderPNo) "
            "VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s)",
            (
                MAIN_ORDER,
                index,
                planned,
                MAIN_WP,
                MAIN_STEP,
                RASS3_RESOURCE_ID,
                RASS3_FEED_OP,
                MAIN_WP,
                MAIN_WP,
            ),
        )
        db.execute(
            "INSERT INTO tblstep "
            "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
            " ResourceID, PlannedStart, Active) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, 1)",
            (
                MAIN_WP,
                MAIN_STEP,
                MAIN_ORDER,
                index,
                "feed part for RASS3",
                RASS3_FEED_OP,
                RASS3_RESOURCE_ID,
                planned,
            ),
        )
        db.execute(
            "INSERT INTO tblstepparameter "
            "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
            "VALUES (%s, %s, %s, %s, 3, %s, 2)",
            (MAIN_WP, MAIN_STEP, MAIN_ORDER, index, str(PART_NO)),
        )
        demands.add((MAIN_ORDER, index, MAIN_STEP))
    return demands


def _seed_source_box(db: Any, box_id: int = 600, asrs_pos: int = 5) -> None:
    """A full ASRS source box with eight slots of the demanded part."""
    for box_pos in range(1, BOX_CAPACITY + 1):
        db.execute(
            "INSERT INTO tblboxpos "
            "(BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
            "VALUES (%s, %s, %s, 0, 0, %s, 9)",
            (box_pos, BOX_TYPE_PNO, box_id, PART_NO),
        )
    db.execute(
        "UPDATE tblbufferpos SET PNo = %s, BoxID = %s, TimeStamp = NOW() "
        "WHERE ResourceId = %s AND BufNo = 1 AND BufPos = %s",
        (BOX_TYPE_PNO, box_id, ASRS_RESOURCE_ID, asrs_pos),
    )


def _seed_source_box_in_pick_sort(db: Any, box_id: int = 601, buf_no: int = 2) -> None:
    """A part box physically sitting in a disabled Pick&Sort buffer."""
    for box_pos in range(1, BOX_CAPACITY + 1):
        db.execute(
            "INSERT INTO tblboxpos "
            "(BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
            "VALUES (%s, %s, %s, 0, 0, %s, 9)",
            (box_pos, BOX_TYPE_PNO, box_id, PART_NO),
        )
    db.execute(
        "UPDATE tblbufferpos SET PNo = %s, BoxID = %s, TimeStamp = NOW() "
        "WHERE ResourceId = %s AND BufNo = %s AND BufPos = 1",
        (BOX_TYPE_PNO, box_id, PICK_AND_SORT_RESOURCE_ID, buf_no),
    )


def _seed_prior_source_link(db: Any, box_id: int = 601) -> None:
    """Make ``box_id`` the preferred (already-used) source for PART_NO."""
    db.execute(
        "INSERT INTO tblstepparameter "
        "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
        "VALUES (%s, 10, 7000, 1, 3, %s, 2)",
        (RASS3_WORKPLAN, str(box_id)),
    )
    db.execute(
        "INSERT INTO tblstepparameter "
        "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
        "VALUES (%s, 10, 7000, 1, 11, %s, 2)",
        (RASS3_WORKPLAN, str(PART_NO)),
    )


def _seed_active_commissioning_order(
    db: Any,
    *,
    order_number: int = ACTIVE_COMMISSIONING_ORDER,
    box_id: int = 500,
    slot: int = 1,
) -> None:
    """One open WP3 commissioning position in ``box_id``.

    It is deliberately linked to a main order that is not one of the five
    demands, so all five demands are still eligible for planning.
    """
    main_ono, main_opos, main_step = ACTIVE_COMMISSIONING_LINK
    now = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) "
        "VALUES (%s, %s, 0, 0, 1)",
        (order_number, now),
    )
    db.execute(
        "INSERT INTO tblorderpos "
        "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
        " OpNo, PNo, OrderPNo) "
        "VALUES (%s, 1, %s, %s, 10, 0, 0, %s, 220, %s, %s)",
        (order_number, now, RASS3_WORKPLAN, PICK_AND_SORT_RESOURCE_ID, PART_NO, RASS3_WORKPLAN),
    )
    db.execute(
        "INSERT INTO tblstep "
        "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
        " ResourceID, PlannedStart, Active) "
        "VALUES (%s, 10, %s, 1, %s, 220, 20, 1, %s, %s, 1)",
        (RASS3_WORKPLAN, order_number, "commissioning", PICK_AND_SORT_RESOURCE_ID, now),
    )

    parameters = {
        1: "0",
        2: "0",
        3: "0",
        4: "0",
        5: "0",
        6: str(box_id),
        7: str(slot),
        8: str(main_ono),
        9: str(main_opos),
        10: str(main_step),
        11: str(PART_NO),
    }
    for parameter_no, value in parameters.items():
        db.execute(
            "INSERT INTO tblstepparameter "
            "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
            "VALUES (%s, 10, %s, 1, %s, %s, 2)",
            (RASS3_WORKPLAN, order_number, parameter_no, value),
        )
    db.execute(
        "UPDATE tblboxpos SET ONo = %s, OPos = 1 WHERE BoxId = %s AND BoxPos = %s",
        (order_number, box_id, slot),
    )


def _occupy_box_slots_with_parts(
    db: Any, box_id: int, positions: range, *, part_no: int = 832
) -> None:
    """Mark slots as occupied by an already commissioned part."""
    for box_pos in positions:
        db.execute(
            "UPDATE tblboxpos SET ONo = 0, OPos = 0, PNo = %s "
            "WHERE BoxId = %s AND BoxPos = %s",
            (part_no, box_id, box_pos),
        )


def _seed_world(db: Any, *, free_slots: int = 2) -> dict[str, Any]:
    """The shared test world: five demands, one active order, two free slots."""
    demands = _seed_main_demands(db)
    _seed_source_box(db)
    _seed_active_commissioning_order(db, box_id=500, slot=1)

    # Slot 1 is already used by the active order.  Fill the remaining
    # capacity-free_slots slots so exactly ``free_slots`` positions are free.
    occupied_other = BOX_CAPACITY - free_slots - 1
    if occupied_other > 0:
        _occupy_box_slots_with_parts(db, 500, range(2, 2 + occupied_other))
    return {"demands": demands, "box_id": 500, "new_box_id": 501}


def _commission_positions(db: Any) -> set[tuple[int, int]]:
    rows = db.query(
        "SELECT ONo, OPos FROM tblorderpos WHERE WPNo = %s AND StepNo = 10",
        (RASS3_WORKPLAN,),
    )
    return {(int(row["ONo"]), int(row["OPos"])) for row in rows}


def _commission_rows(db: Any) -> list[dict[str, Any]]:
    """Commissioning positions with their StepNo-10 parameters, pivoted."""
    return db.query(
        """
        SELECT
            op.ONo AS ono,
            op.OPos AS opos,
            op.OpNo AS opno,
            op.ResourceID AS resource_id,
            op.`End` AS end_time,
            op.State AS state,
            MAX(CASE WHEN sp.ParameterNo = 3 THEN sp.Parameter END) AS p3,
            MAX(CASE WHEN sp.ParameterNo = 4 THEN sp.Parameter END) AS p4,
            MAX(CASE WHEN sp.ParameterNo = 6 THEN sp.Parameter END) AS p6,
            MAX(CASE WHEN sp.ParameterNo = 7 THEN sp.Parameter END) AS p7,
            MAX(CASE WHEN sp.ParameterNo = 8 THEN sp.Parameter END) AS p8,
            MAX(CASE WHEN sp.ParameterNo = 9 THEN sp.Parameter END) AS p9,
            MAX(CASE WHEN sp.ParameterNo = 10 THEN sp.Parameter END) AS p10,
            MAX(CASE WHEN sp.ParameterNo = 11 THEN sp.Parameter END) AS p11
        FROM
            tblorderpos op
            LEFT JOIN tblstepparameter sp ON
                sp.ONo = op.ONo AND
                sp.OPos = op.OPos AND
                sp.StepNo = 10
        WHERE
            op.WPNo = %s AND
            op.StepNo = 10
        GROUP BY
            op.ONo, op.OPos, op.OpNo, op.ResourceID, op.`End`, op.State
        ORDER BY
            op.ONo, op.OPos
        """,
        (RASS3_WORKPLAN,),
    )


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _link(row: dict[str, Any]) -> tuple[int, int, int] | None:
    values = (_to_int(row["p8"]), _to_int(row["p9"]), _to_int(row["p10"]))
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _target_slot_map(db: Any, box_id: int) -> dict[int, tuple[int, int, int]]:
    rows = db.query(
        "SELECT BoxPos, ONo, OPos, PNo FROM tblboxpos "
        "WHERE BoxId = %s AND BoxPNo = %s ORDER BY BoxPos",
        (box_id, BOX_TYPE_PNO),
    )
    return {
        int(row["BoxPos"]): (int(row["ONo"]), int(row["OPos"]), int(row["PNo"]))
        for row in rows
    }


def _assert_commission_invariants(
    db: Any,
    new_positions: set[tuple[int, int]],
    *,
    expected_target_box: int | None = None,
) -> None:
    """Invariants that must hold after every planning cycle.

    ``new_positions`` are the positions created during that cycle, identified
    by diffing the table before and after the CLI run.  Existing positions may
    have been completed between cycles, so the target-slot existence check is
    applied to open positions and to every newly created position; the
    parameter-7 and uniqueness invariants apply globally.
    """
    rows = _commission_rows(db)
    by_position = {(int(row["ono"]), int(row["opos"])): row for row in rows}
    missing = new_positions - set(by_position)
    assert not missing, f"newly created positions disappeared: {sorted(missing)}"

    pos_count = int(
        db.scalar(
            "SELECT PosCount FROM tblboxdef WHERE BoxPNo = %s",
            (BOX_TYPE_PNO,),
            default=BOX_CAPACITY,
        )
        or BOX_CAPACITY
    )

    # A commissioning position may never carry ParameterNo 7 = 0 (or empty).
    zero_parameter_7 = db.scalar(
        """
        SELECT COUNT(*)
        FROM
            tblstepparameter sp
            INNER JOIN tblorderpos op ON
                op.ONo = sp.ONo AND
                op.OPos = sp.OPos AND
                op.StepNo = sp.StepNo
        WHERE
            sp.StepNo = 10 AND
            sp.ParameterNo = 7 AND
            CAST(COALESCE(sp.Parameter, '0') AS UNSIGNED) = 0 AND
            op.WPNo = %s
        """,
        (RASS3_WORKPLAN,),
    )
    assert zero_parameter_7 == 0, (
        "the planner created/wrote ParameterNo 7 = 0; a zero target position "
        "makes MES4 lose the part"
    )

    # No box slot may carry two different order positions.  (The boxpos PK
    # already prevents two rows for one slot; this catches bad joins/rewrites.)
    conflicting_slots = db.query(
        """
        SELECT BoxId, BoxPos, COUNT(DISTINCT CONCAT(ONo, ':', OPos)) AS n
        FROM tblboxpos
        GROUP BY BoxId, BoxPos
        HAVING n > 1
        """
    )
    assert not conflicting_slots, f"box slots carry multiple order positions: {conflicting_slots}"

    # Every commission has a valid target box/slot; open commissions and new
    # positions must actually own the corresponding tblboxpos row.
    open_targets: dict[tuple[int, int], tuple[int, int]] = {}
    for position, row in by_position.items():
        box_id = _to_int(row["p6"])
        slot = _to_int(row["p7"])
        assert box_id is not None and box_id > 0, f"position {position} has no target box: {row}"
        assert slot is not None and 1 <= slot <= pos_count, (
            f"position {position} has invalid ParameterNo 7={row['p7']!r}; "
            f"expected 1..{pos_count}"
        )
        if expected_target_box is not None and position in new_positions:
            assert box_id == expected_target_box, (
                f"new position {position} was booked into box {box_id}, "
                f"expected {expected_target_box}"
            )
        is_open = row["end_time"] is None
        if is_open or position in new_positions:
            key = (box_id, slot)
            assert key not in open_targets, (
                f"target slot {key} is used by both {open_targets.get(key)} and {position}"
            )
            open_targets[key] = position
            slot_row = _target_slot_map(db, box_id).get(slot)
            assert slot_row is not None, (
                f"position {position} points at missing box slot {box_id}.{slot}"
            )
            assert slot_row[0] == position[0] and slot_row[1] == position[1], (
                f"position {position} points at box slot {box_id}.{slot} holding "
                f"{slot_row[0]}.{slot_row[1]}"
            )

    # Booked slots per box never exceed the box definition's PosCount.
    overfull = db.query(
        """
        SELECT b.BoxId, COUNT(*) AS booked, d.PosCount
        FROM
            tblboxpos b
            INNER JOIN tblboxdef d ON d.BoxPNo = b.BoxPNo
        WHERE
            b.BoxPNo = %s AND
            (b.ONo <> 0 OR b.OPos <> 0 OR b.PNo <> 0)
        GROUP BY b.BoxId, d.PosCount
        HAVING COUNT(*) > d.PosCount
        """,
        (BOX_TYPE_PNO,),
    )
    assert not overfull, f"box(es) overbooked beyond PosCount: {overfull}"

    # Every new commissioning order position is a real commissioning position
    # and has the complete StepNo-10 parameter set created by MES4.
    for position in new_positions:
        row = by_position[position]
        assert _to_int(row["opno"]) == 220, f"{position} is not op 220: {row}"
        assert _to_int(row["resource_id"]) == PICK_AND_SORT_RESOURCE_ID, (
            f"{position} is not at Pick & Sort: {row}"
        )
        count = db.scalar(
            "SELECT COUNT(*) FROM tblstepparameter WHERE StepNo = 10 AND ONo = %s AND OPos = %s",
            (position[0], position[1]),
        )
        assert count == 11, (
            f"{position} has {count} StepNo-10 parameter rows, expected 11"
        )


def _complete_target_box(db: Any, box_id: int) -> None:
    """Simulate delivery + commissioning completion and empty the box.

    All WP3 commissioning positions are ended and their orders disabled, the
    box slots are cleared, and the box is removed from the ASRS20 buffer so a
    later cycle must select the next empty box.
    """
    onos = [
        int(row["ONo"])
        for row in db.query("SELECT DISTINCT ONo FROM tblorderpos WHERE WPNo = %s", (RASS3_WORKPLAN,))
    ]
    db.execute(
        "UPDATE tblorderpos SET `End` = NOW(), State = 100 WHERE WPNo = %s",
        (RASS3_WORKPLAN,),
    )
    for order_number in onos:
        db.execute(
            "UPDATE tblorder SET `End` = NOW(), State = 100, Enabled = 0 WHERE ONo = %s",
            (order_number,),
        )
    db.execute(
        "UPDATE tblboxpos SET ONo = 0, OPos = 0, PNo = 0 WHERE BoxId = %s",
        (box_id,),
    )
    db.execute(
        "UPDATE tblbufferpos SET PNo = 0, BoxID = 0, TimeStamp = NOW() "
        "WHERE ResourceId = %s AND BoxID = %s",
        (ASRS_RESOURCE_ID, box_id),
    )


def _set_direct_delivery(planner_env: Any, name: str, mode: str) -> None:
    """Switch one resource to direct delivery in the temporary config."""
    data = json.loads(planner_env.config_path.read_text(encoding="utf-8"))
    for resource in data["resources"]:
        if resource["name"] == name:
            resource["directDelivery"] = mode
            break
    else:  # pragma: no cover - the fixture always defines RASS1
        raise AssertionError(f"resource {name} is not in the test config")
    planner_env.config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _seed_station_buffer(db: Any, resource_id: int) -> None:
    now = datetime.now().replace(microsecond=0)
    for buf_pos in (1, 2):
        db.execute(
            "INSERT IGNORE INTO tblbufferpos "
            "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, "
            " QuantityMax, TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
            "VALUES (%s, 1, %s, 0, 0, 0, 12, 7, 0, 0, %s, 0, 0, 7, 0)",
            (resource_id, buf_pos, now),
        )


def _seed_rass1_feed_position(
    db: Any,
    *,
    order_number: int = 9000,
    order_position: int = 1,
    parts: tuple[int, ...] = (800, 814),
) -> None:
    """One open RASS1 main position whose feed steps need ``parts``."""
    now = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) "
        "VALUES (%s, %s, 0, 0, 1)",
        (order_number, now),
    )
    db.execute(
        "INSERT INTO tblorderpos "
        "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
        " OpNo, PNo, OrderPNo) "
        "VALUES (%s, %s, %s, %s, 160, 0, 0, %s, %s, %s, %s)",
        (
            order_number,
            order_position,
            now,
            MAIN_WP,
            RASS1_RESOURCE_ID,
            RASS1_FEED_OP,
            MAIN_WP,
            MAIN_WP,
        ),
    )
    for index, part_no in enumerate(parts):
        step_no = 160 + index * 10
        db.execute(
            "INSERT INTO tblstep "
            "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
            " ResourceID, PlannedStart, Active) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, 1)",
            (
                MAIN_WP,
                step_no,
                order_number,
                order_position,
                f"feed part {part_no}",
                RASS1_FEED_OP,
                RASS1_RESOURCE_ID,
                now,
            ),
        )
        db.execute(
            "INSERT INTO tblstepparameter "
            "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
            "VALUES (%s, %s, %s, %s, 3, %s, 2)",
            (MAIN_WP, step_no, order_number, order_position, str(part_no)),
        )
    _seed_station_buffer(db, RASS1_RESOURCE_ID)


def _seed_fully_unbooked_asrs_box(
    db: Any,
    *,
    box_id: int = 600,
    asrs_pos: int = 3,
    parts: tuple[int, ...] = (800, 814),
) -> None:
    """An ASRS20 box with ``parts`` in the first slots and no booking at all."""
    for box_pos in range(1, BOX_CAPACITY + 1):
        part_no = parts[box_pos - 1] if box_pos <= len(parts) else 0
        db.execute(
            "INSERT INTO tblboxpos "
            "(BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
            "VALUES (%s, %s, %s, 0, 0, %s, 9)",
            (box_pos, BOX_TYPE_PNO, box_id, part_no),
        )
    db.execute(
        "UPDATE tblbufferpos SET PNo = %s, BoxID = %s, TimeStamp = NOW() "
        "WHERE ResourceId = %s AND BufNo = 1 AND BufPos = %s",
        (BOX_TYPE_PNO, box_id, ASRS_RESOURCE_ID, asrs_pos),
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_capacity_never_writes_param7_zero(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """A box with two free slots gets exactly two positions, never P7=0."""
    db = clean_db
    world = _seed_world(db, free_slots=2)
    demands: set[tuple[int, int, int]] = world["demands"]

    before = _commission_positions(db)
    planner_env.run("plan")
    new = _commission_positions(db) - before
    new_rows = [
        row for row in _commission_rows(db) if (int(row["ono"]), int(row["opos"])) in new
    ]

    assert len(new) == 2, (
        f"target box 500 had 2 free slots but {len(new)} commissioning positions were created: "
        f"{sorted(new)}"
    )
    _assert_commission_invariants(db, new, expected_target_box=500)

    new_links = {_link(row) for row in new_rows} - {None}
    assert len(new_links) == 2, f"two positions should serve two distinct demands, got {new_links}"
    assert new_links <= demands, f"planning created links outside the five demands: {new_links}"

    target_pairs = {(_to_int(row["p6"]), _to_int(row["p7"])) for row in new_rows}
    assert len(target_pairs) == 2, f"new positions share a target slot: {target_pairs}"
    assert all(box == 500 for box, _ in target_pairs), target_pairs
    assert all(slot is not None and 1 <= slot <= BOX_CAPACITY for _, slot in target_pairs)

    target_slots = _target_slot_map(db, 500)
    rows_by_position = {
        (int(row["ono"]), int(row["opos"])): row for row in new_rows
    }
    for position in sorted(new):
        slot = _to_int(rows_by_position[position]["p7"])
        assert slot is not None
        assert target_slots[slot][:2] == position, (
            f"box 500 slot {slot} holds {target_slots[slot][:2]}, expected {position}"
        )

    free_after = db.scalar(
        "SELECT COUNT(*) FROM tblboxpos WHERE BoxId = 500 AND ONo = 0 AND OPos = 0 AND PNo = 0"
    )
    assert free_after == 0, "box 500 should be full after booking its two free slots"

    # Simulate delivery/completion, then run a later cycle.  The two already
    # served main steps stay commissioned; the remaining three get a new box.
    _complete_target_box(db, 500)
    before_second = _commission_positions(db)
    planner_env.run("plan")
    new_second = _commission_positions(db) - before_second
    new_second_rows = [
        row
        for row in _commission_rows(db)
        if (int(row["ono"]), int(row["opos"])) in new_second
    ]

    assert len(new_second) == 3, (
        f"after completing the first two demands, {len(new_second)} positions were created "
        f"for the remaining three (expected 3): {sorted(new_second)}"
    )
    _assert_commission_invariants(db, new_second, expected_target_box=501)

    second_links = {_link(row) for row in new_second_rows} - {None}
    assert second_links == demands - new_links, (
        "the later cycle did not plan exactly the three still-open demands: "
        f"planned={second_links}, expected={demands - new_links}"
    )
    second_pairs = {(_to_int(row["p6"]), _to_int(row["p7"])) for row in new_second_rows}
    assert len(second_pairs) == 3, f"later-cycle positions share a target slot: {second_pairs}"
    assert all(box == 501 for box, _ in second_pairs), (
        f"remaining demands should be planned into the next empty box 501: {second_pairs}"
    )

    all_planned = {
        link
        for link in (_link(row) for row in _commission_rows(db))
        if link in demands
    }
    assert all_planned == demands, (
        f"all five demands should be commissioned exactly once, got {all_planned}"
    )


def test_repeated_cycles_no_double_booking_or_param7_zero(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """Repeated cycles keep the invariants and do not duplicate bookings."""
    db = clean_db
    world = _seed_world(db, free_slots=2)
    demands: set[tuple[int, int, int]] = world["demands"]

    # Two cycles while box 500 is active: the first fills its two free slots,
    # the second must do nothing because the box is full.
    for cycle in range(1, 3):
        before = _commission_positions(db)
        planner_env.run("plan")
        new = _commission_positions(db) - before
        _assert_commission_invariants(db, new)
        if cycle == 1:
            assert len(new) == 2, f"cycle 1 created {len(new)} positions, expected 2"
        else:
            assert new == set(), f"cycle 2 re-booked into a full box: {sorted(new)}"

    first_links = {
        link
        for link in (_link(row) for row in _commission_rows(db))
        if link in demands
    }
    assert len(first_links) == 2

    # Complete the box, then run two more cycles: three remaining demands go
    # into the next empty box; the following cycle is a no-op.
    _complete_target_box(db, 500)
    for cycle in range(3, 5):
        before = _commission_positions(db)
        planner_env.run("plan")
        new = _commission_positions(db) - before
        _assert_commission_invariants(db, new)
        if cycle == 3:
            assert len(new) == 3, f"cycle 3 created {len(new)} positions, expected 3"
        else:
            assert new == set(), f"cycle 4 duplicated already commissioned demands: {sorted(new)}"

    all_planned = {
        link
        for link in (_link(row) for row in _commission_rows(db))
        if link in demands
    }
    assert all_planned == demands, f"not every demand was commissioned exactly once: {all_planned}"

    # And one more fresh world, to prove the invariants do not depend on rows
    # left over from the earlier cycles.
    db.reset()
    _seed_world(db, free_slots=2)
    before = _commission_positions(db)
    planner_env.run("plan")
    new = _commission_positions(db) - before
    assert len(new) == 2, f"fresh cycle created {len(new)} positions, expected 2"
    _assert_commission_invariants(db, new)


def test_buffer_blocking_maps_bufno(
    clean_db: Any,
    fake_mes: Any,
    fake_fleet_manager: Any,
    planner_env: Any,
) -> None:
    """151/15 and 151/16 must use real BufNo values, not array indexes."""
    db = clean_db

    # Non-contiguous buffer 3 is a Robotino target; buffer 5 is its source.
    # A finished task to buffer 1 must be ignored.
    fake_fleet_manager.tasks = [
        {
            "MESFromResource": PICK_AND_SORT_RESOURCE_ID,
            "MESFromBufNo": 5,
            "MESToResource": PICK_AND_SORT_RESOURCE_ID,
            "MESToBufNo": 3,
            "TaskStatus": "Started",
        },
        {
            # Not a Pick & Sort task: it must not lock any buffer.
            "MESFromResource": 18,
            "MESFromBufNo": 1,
            "MESToResource": ASRS_RESOURCE_ID,
            "MESToBufNo": 1,
            "TaskStatus": "Error",
        },
        {
            "MESFromResource": ASRS_RESOURCE_ID,
            "MESFromBufNo": 1,
            "MESToResource": PICK_AND_SORT_RESOURCE_ID,
            "MESToBufNo": 1,
            "TaskStatus": "Finished",
        },
    ]

    before = len(fake_mes.commands)
    planner_env.run("block")
    commands = [c for c in fake_mes.commands[before:] if c.mclass == 151]

    assert commands, "block did not send any 151/15 or 151/16 requests"
    assert fake_fleet_manager.requests, "block did not query the Fleet Manager"
    assert any("GetTaskHistory" in path for path in fake_fleet_manager.requests), (
        f"unexpected Fleet Manager paths: {fake_fleet_manager.requests}"
    )

    final_action: dict[int, int] = {}
    for command in commands:
        assert command.mno in (15, 16), command
        assert command.resource_id == PICK_AND_SORT_RESOURCE_ID, command
        assert command.params.get("BufPos") == "1", command
        assert command.buf_no is not None and 1 <= command.buf_no <= 6, command
        final_action[command.buf_no] = command.mno

    booked = {
        int(row["BufNo"]): int(row["Booked"])
        for row in db.query(
            "SELECT BufNo, Booked FROM tblbufferpos "
            "WHERE ResourceId = %s AND BufPos = 1",
            (PICK_AND_SORT_RESOURCE_ID,),
        )
    }

    # Each request must describe the buffer whose final state it changed.
    for buf_no, mno in final_action.items():
        expected = 1 if mno == 15 else 0
        assert booked[buf_no] == expected, (
            f"151/{mno} for BufNo {buf_no} left Booked={booked[buf_no]}, expected {expected}"
        )

    # Buffer 3 is non-contiguous and a Robotino target; it must have been
    # locked by a 151/15 carrying BufNo=3 (not by position 3 of an array).
    assert 3 in final_action, f"Robotino target buffer 3 was not touched: {final_action}"
    assert final_action[3] == 15, f"Robotino target buffer 3 was unlocked: {final_action}"
    assert final_action.get(5) == 15, f"Robotino source buffer 5 was not locked: {final_action}"

    assert booked.get(2) == 1, "disabled buffer 2 must stay locked"
    assert booked.get(6) == 1, "disabled buffer 6 must stay locked"
    assert booked.get(3) == 1, "Robotino target buffer 3 must stay locked"
    assert booked.get(5) == 1, "Robotino source buffer 5 must stay locked"

    unlocked_non_disabled = [
        buf_no
        for buf_no in range(1, 7)
        if buf_no not in (2, 6) and booked.get(buf_no) == 0
    ]
    assert len(unlocked_non_disabled) <= 2, (
        f"more than two non-disabled buffers are unlocked: {unlocked_non_disabled}"
    )


def _seed_aass_remove_step(db: Any, *, order_number: int = 9100, order_position: int = 1) -> None:
    """One open AASS remove step (op 620) with no target box yet."""
    now = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) "
        "VALUES (%s, %s, 0, 0, 1)",
        (order_number, now),
    )
    db.execute(
        "INSERT INTO tblorderpos "
        "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
        " OpNo, PNo, OrderPNo) "
        "VALUES (%s, %s, %s, %s, 250, 0, 0, 26, 620, 1000, 1000)",
        (order_number, order_position, now, MAIN_WP),
    )
    db.execute(
        "INSERT INTO tblstep "
        "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
        " ResourceID, PlannedStart, Active) "
        "VALUES (%s, 250, %s, %s, %s, 620, 0, 0, 26, %s, 1)",
        (MAIN_WP, order_number, order_position, "remove part (AASS)", now),
    )
    # AASS1 station buffer: both positions empty.
    for buf_pos in (1, 2):
        db.execute(
            "INSERT IGNORE INTO tblbufferpos "
            "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, "
            " QuantityMax, TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
            "VALUES (26, 1, %s, 0, 0, 0, 12, 7, 0, 0, %s, 0, 0, 7, 0)",
            (buf_pos, now),
        )


def _seed_finished_disassembly_box(
    db: Any, *, order_number: int = 9001, box_id: int = 500, used_slots: int = 6
) -> None:
    """A partly used disassembly box left by an earlier, finished remove step."""
    now = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, `End`, CNo, State, Enabled) "
        "VALUES (%s, %s, %s, 0, 100, 1)",
        (order_number, now, now),
    )
    db.execute(
        "INSERT INTO tblorderpos "
        "(ONo, OPos, PlannedStart, `End`, WPNo, StepNo, MainOPos, State, "
        " ResourceID, OpNo, PNo, OrderPNo) "
        "VALUES (%s, 1, %s, %s, %s, 250, 0, 100, 26, 620, 1000, 1000)",
        (order_number, now, now, MAIN_WP),
    )
    db.execute(
        "INSERT INTO tblstep "
        "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
        " ResourceID, PlannedStart, `End`, Active) "
        "VALUES (%s, 250, %s, 1, %s, 620, 0, 0, 26, %s, %s, 1)",
        (MAIN_WP, order_number, "finished remove (AASS)", now, now),
    )
    for box_pos in range(1, used_slots + 1):
        db.execute(
            "UPDATE tblboxpos SET ONo = %s, OPos = 1, PNo = 0 "
            "WHERE BoxId = %s AND BoxPos = %s",
            (order_number, box_id, box_pos),
        )


def test_disassembly_reuses_existing_box_up_to_poscount(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """`whichBoxHasDisassemblyOrder` uses PosCount and reuses a partly full box."""
    db = clean_db
    _seed_finished_disassembly_box(db, used_slots=6)
    _seed_aass_remove_step(db, order_number=9100, order_position=1)
    _seed_aass_remove_step(db, order_number=9101, order_position=1)
    before_commands = len(fake_mes.commands)

    planner_env.run("plan")

    # Box 500 had 8 - 6 = 2 free slots, exactly the two new requests.
    slots = db.query(
        "SELECT BoxPos, ONo, OPos FROM tblboxpos "
        "WHERE BoxId = 500 AND ONo IN (9100, 9101) ORDER BY BoxPos"
    )
    assert slots == [
        {"BoxPos": 7, "ONo": 9100, "OPos": 1},
        {"BoxPos": 8, "ONo": 9101, "OPos": 1},
    ], slots
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "the AASS no-store path must not create a MES4 order"


def _seed_palrob_remove_step(
    db: Any, *, order_number: int = 9200, order_position: int = 1
) -> None:
    """One open PALROB remove step (op 351) with no target box yet."""
    now = datetime.now().replace(microsecond=0)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) "
        "VALUES (%s, %s, 0, 0, 1)",
        (order_number, now),
    )
    db.execute(
        "INSERT INTO tblorderpos "
        "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
        " OpNo, PNo, OrderPNo) "
        "VALUES (%s, %s, %s, %s, 260, 0, 0, 15, 351, 1000, 1000)",
        (order_number, order_position, now, MAIN_WP),
    )
    db.execute(
        "INSERT INTO tblstep "
        "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
        " ResourceID, PlannedStart, Active) "
        "VALUES (%s, 260, %s, %s, %s, 351, 0, 0, 15, %s, 1)",
        (MAIN_WP, order_number, order_position, "remove part (PALROB)", now),
    )
    for buf_pos in (1, 2):
        db.execute(
            "INSERT IGNORE INTO tblbufferpos "
            "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, "
            " QuantityMax, TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
            "VALUES (15, 1, %s, 0, 0, 0, 12, 7, 0, 0, %s, 0, 0, 7, 0)",
            (buf_pos, now),
        )


def test_disassembly_creates_a_tcp_order_and_books_a_slot(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """PALROB disassembly uses 101/6 and books the returned ONo/OPos."""
    db = clean_db
    _seed_palrob_remove_step(db)
    before_commands = len(fake_mes.commands)

    planner_env.run("plan")

    commands = [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101 and command.mno == 6
    ]
    assert len(commands) == 1, commands
    assert commands[0].string == "9-9200-1", commands[0].string

    order = db.query_one(
        "SELECT ONo, OPos FROM tblorderpos WHERE WPNo = 9 AND StepNo = 10"
    )
    assert order is not None, "fake MES4 did not create the disassembly order"
    slot = db.query_one(
        "SELECT ONo, OPos, PNo FROM tblboxpos WHERE BoxId = 500 AND BoxPos = 1"
    )
    assert slot == {"ONo": int(order["ONo"]), "OPos": int(order["OPos"]), "PNo": 0}, slot


def test_disassembly_without_store_books_an_empty_slot(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """AASS remove steps use the no-TCP path and a real empty target slot."""
    db = clean_db
    _seed_aass_remove_step(db)
    before_commands = len(fake_mes.commands)

    planner_env.run("plan")

    slot = db.query_one(
        "SELECT ONo, OPos, PNo FROM tblboxpos "
        "WHERE BoxId = 500 AND BoxPos = 1"
    )
    assert slot == {"ONo": 9100, "OPos": 1, "PNo": 0}, slot
    booked = db.scalar(
        "SELECT COUNT(*) FROM tblboxpos WHERE BoxId = 500 AND ONo <> 0"
    )
    assert booked == 1, f"expected one booked disassembly slot, got {booked}"
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "the AASS no-store path must not create a MES4 order"


def test_ambiguous_active_orders_skip_resource(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """Two active boxes/orders for one resource must be skipped, not guessed."""
    db = clean_db
    _seed_main_demands(db)
    _seed_source_box(db)
    _seed_active_commissioning_order(db, order_number=7001, box_id=500, slot=1)
    _seed_active_commissioning_order(db, order_number=7002, box_id=501, slot=1)

    before = _commission_positions(db)
    before_commands = len(fake_mes.commands)

    result = planner_env.run("plan")

    assert _commission_positions(db) == before, (
        "an ambiguous active order produced new commissioning positions"
    )
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "an ambiguous active order still created a MES4 order"
    assert "Cannot plan RASS3" in result.stdout, result.stdout


def test_disabled_buffer_is_relocked_when_fleet_manager_is_down(
    clean_db: Any,
    fake_mes: Any,
    fake_fleet_manager: Any,
    planner_env: Any,
) -> None:
    """An operator/MES reset of BufNo 2 must not stay open during an FM outage."""
    db = clean_db
    # Simulate EditBufferPos/ShowBuffer resetting the broken buffer to Booked=0.
    db.execute(
        "UPDATE tblbufferpos SET Booked = 0 "
        "WHERE ResourceId = %s AND BufNo = 2 AND BufPos = 1",
        (PICK_AND_SORT_RESOURCE_ID,),
    )
    fake_fleet_manager.stop()
    before_commands = len(fake_mes.commands)

    planner_env.run("block")

    booked = {
        int(row["BufNo"]): int(row["Booked"])
        for row in db.query(
            "SELECT BufNo, Booked FROM tblbufferpos "
            "WHERE ResourceId = %s AND BufPos = 1",
            (PICK_AND_SORT_RESOURCE_ID,),
        )
    }
    assert booked.get(2) == 1, "disabled buffer 2 was left open while FM was down"
    assert booked.get(6) == 1, "disabled buffer 6 was left open while FM was down"

    commands = fake_mes.commands[before_commands:]
    assert commands, "block sent no lock command at all"
    assert all(command.mclass == 151 and command.mno == 15 for command in commands), (
        commands
    )
    assert any(command.buf_no == 2 for command in commands), commands


def test_source_box_in_disabled_buffer_is_not_chosen(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """A box stuck in BufNo 2 must never become a source box."""
    db = clean_db
    _seed_source_box(db, box_id=600, asrs_pos=5)
    _seed_source_box_in_pick_sort(db, box_id=601, buf_no=2)
    _seed_prior_source_link(db, box_id=601)
    _seed_active_commissioning_order(db, order_number=7001, box_id=500, slot=1)

    planner_env.run("plan")

    source_params = db.query_one(
        "SELECT "
        "MAX(CASE WHEN ParameterNo = 3 THEN Parameter END) AS p3, "
        "MAX(CASE WHEN ParameterNo = 4 THEN Parameter END) AS p4 "
        "FROM tblstepparameter "
        "WHERE ONo = 7001 AND OPos = 1 AND StepNo = 10"
    )
    assert source_params is not None
    assert source_params["p3"] == "600", source_params
    assert source_params["p4"] is not None, source_params
    booked_disabled = db.scalar(
        "SELECT COUNT(*) FROM tblboxpos WHERE BoxId = 601 AND ONo <> 0"
    )
    assert booked_disabled == 0, "a slot in disabled-box 601 was booked"
    booked_asrs = db.scalar(
        "SELECT COUNT(*) FROM tblboxpos WHERE BoxId = 600 AND ONo = 7001"
    )
    assert booked_asrs == 1, "the source box in ASRS20 was not used"


def test_planted_orphan_position_is_reconciled(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """A param-7=0 orphan from the old planner is repaired on the next cycle."""
    db = clean_db
    _seed_source_box(db)
    _seed_active_commissioning_order(db, order_number=7001, box_id=500, slot=1)
    # Remove the target slot and reset param 7 to the old broken value.
    db.execute(
        "UPDATE tblboxpos SET ONo = 0, OPos = 0 "
        "WHERE BoxId = 500 AND BoxPos = 1"
    )
    db.execute(
        "UPDATE tblstepparameter SET Parameter = '0' "
        "WHERE ONo = 7001 AND OPos = 1 AND StepNo = 10 AND ParameterNo = 7"
    )
    before_commands = len(fake_mes.commands)

    planner_env.run("plan")

    slot = db.query_one(
        "SELECT ONo, OPos FROM tblboxpos WHERE BoxId = 500 AND BoxPos = 1"
    )
    assert slot == {"ONo": 7001, "OPos": 1}, slot
    p7 = db.scalar(
        "SELECT Parameter FROM tblstepparameter "
        "WHERE ONo = 7001 AND OPos = 1 AND StepNo = 10 AND ParameterNo = 7"
    )
    assert p7 == "1", p7
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "reconcile created a new MES4 order instead of repairing the slot"


def test_simulate_rolls_back_plan_and_does_not_send_tcp(
    clean_db: Any,
    fake_mes: Any,
    fake_fleet_manager: Any,
    planner_env: Any,
) -> None:
    """Copy-DB simulate mode must leave the DB and the MES4 socket untouched."""
    db = clean_db
    _seed_world(db, free_slots=2)

    before_positions = _commission_positions(db)
    before_box = _target_slot_map(db, 500)
    before_commands = len(fake_mes.commands)

    result = planner_env.run(
        "plan", "--simulate", "--i-know-this-is-a-copy"
    )
    assert "[simulate]" in result.stdout, result.stdout

    assert _commission_positions(db) == before_positions, (
        "simulate plan committed commissioning positions"
    )
    assert _target_slot_map(db, 500) == before_box, "simulate plan changed target box slots"
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "simulate plan contacted MES4"


def test_read_only_dry_run_changes_nothing(
    clean_db: Any,
    fake_mes: Any,
    fake_fleet_manager: Any,
    planner_env: Any,
) -> None:
    """--dry-run is read-only: no DB rows, no locks, no MES4 frames."""
    db = clean_db
    _seed_world(db, free_slots=2)

    before_positions = _commission_positions(db)
    before_box = _target_slot_map(db, 500)
    before_booked = {
        int(row["BufNo"]): int(row["Booked"])
        for row in db.query(
            "SELECT BufNo, Booked FROM tblbufferpos "
            "WHERE ResourceId = %s AND BufPos = 1",
            (PICK_AND_SORT_RESOURCE_ID,),
        )
    }
    before_commands = len(fake_mes.commands)

    result = planner_env.run("plan", "--dry-run")
    assert "[dry-run]" in result.stdout, result.stdout
    assert "would write" in result.stdout, result.stdout

    assert _commission_positions(db) == before_positions
    assert _target_slot_map(db, 500) == before_box
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101
    ], "read-only dry-run contacted MES4"

    result = planner_env.run("block", "--dry-run")
    assert "[dry-run]" in result.stdout, result.stdout
    after_booked = {
        int(row["BufNo"]): int(row["Booked"])
        for row in db.query(
            "SELECT BufNo, Booked FROM tblbufferpos "
            "WHERE ResourceId = %s AND BufPos = 1",
            (PICK_AND_SORT_RESOURCE_ID,),
        )
    }
    assert after_booked == before_booked, "read-only block changed BufferPos.Booked"
    assert not [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 151
    ], "read-only block contacted MES4"

    # No transaction may be left open by the read-only cycle.
    assert db.scalar("SELECT COUNT(*) FROM information_schema.innodb_trx") == 0


def test_read_only_dry_run_books_distinct_slots_like_the_live_planner(
    clean_db: Any,
    fake_mes: Any,
    fake_fleet_manager: Any,
    planner_env: Any,
) -> None:
    """--dry-run must report the same slot assignment a live cycle makes.

    With four free target slots and five demands, the live planner books four
    positions into four different slots.  The read-only shadow run cannot see
    its own writes, so it has to track them per cycle; otherwise every part is
    "booked" into the lowest free slot and the shadow log cannot be compared
    with the old planner's output.
    """
    import re

    db = clean_db
    _seed_world(db, free_slots=4)
    free_before = sorted(
        int(row["BoxPos"])
        for row in db.query(
            "SELECT BoxPos FROM tblboxpos WHERE BoxId = 500 AND BoxPNo = %s "
            "AND ONo = 0 AND OPos = 0 AND PNo = 0",
            (BOX_TYPE_PNO,),
        )
    )
    assert len(free_before) == 4

    result = planner_env.run("plan", "--dry-run")
    booked = [
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for match in re.finditer(
            r"Order (\d+)\.(\d+) is booked to box 500, position (\d+)", result.stdout
        )
    ]
    assert [position for _, _, position in booked] == free_before, result.stdout
    # All four are appended to the active commissioning order with new OPos.
    assert {ono for ono, _, _ in booked} == {ACTIVE_COMMISSIONING_ORDER}
    assert len({opos for _, opos, _ in booked}) == 4
    # And the DB is still untouched.
    assert sorted(
        int(row["BoxPos"])
        for row in db.query(
            "SELECT BoxPos FROM tblboxpos WHERE BoxId = 500 AND BoxPNo = %s "
            "AND ONo = 0 AND OPos = 0 AND PNo = 0",
            (BOX_TYPE_PNO,),
        )
    ) == free_before


def test_direct_delivery_stores_a_covering_asrs_box_without_pick_and_sort(
    clean_db: Any,
    fake_mes: Any,
    planner_env: Any,
) -> None:
    """A fully unbooked box covering the demand goes straight to the station.

    RASS1 is switched to ``prefer``.  The box holds 800 + 814 and is not
    booked to any order, so the planner sends one WP 6 store order
    (``6-<mainONo>-<mainOPos>``) and books both slots to the returned
    sub-order; it must not create a WP 1 Pick & Sort order.
    """
    db = clean_db
    _seed_rass1_feed_position(db)
    _seed_fully_unbooked_asrs_box(db)
    _set_direct_delivery(planner_env, "RASS1", "prefer")
    before_commands = len(fake_mes.commands)
    before_pick_and_sort = db.query(
        "SELECT ONo, OPos FROM tblorderpos WHERE WPNo = %s", (RASS1_WORKPLAN,)
    )

    result = planner_env.run("plan")

    commands = [
        command
        for command in fake_mes.commands[before_commands:]
        if command.mclass == 101 and command.mno == 6
    ]
    assert [command.string for command in commands] == ["6-9000-1"], commands
    assert "direct delivery" in result.stdout, result.stdout

    order = db.query_one(
        "SELECT ONo, OPos FROM tblorderpos WHERE WPNo = %s AND StepNo = 10",
        (RASS1_STORE_WORKPLAN,),
    )
    assert order is not None, "the fake MES4 did not create the WP 6 store order"
    sub_order = (int(order["ONo"]), int(order["OPos"]))

    slots = db.query(
        "SELECT BoxPos, ONo, OPos, PNo FROM tblboxpos "
        "WHERE BoxId = 600 ORDER BY BoxPos"
    )
    assert slots[0] == {
        "BoxPos": 1,
        "ONo": sub_order[0],
        "OPos": sub_order[1],
        "PNo": 800,
    }, slots
    assert slots[1] == {
        "BoxPos": 2,
        "ONo": sub_order[0],
        "OPos": sub_order[1],
        "PNo": 814,
    }, slots
    # The box is stored as a whole; the slots are re-booked to the main
    # position later by op 215's op-end SQL.  Nothing else may be booked.
    assert all(
        slot["ONo"] == 0 and slot["OPos"] == 0 for slot in slots[2:]
    ), slots

    assert (
        db.query(
            "SELECT ONo, OPos FROM tblorderpos WHERE WPNo = %s", (RASS1_WORKPLAN,)
        )
        == before_pick_and_sort
    ), "direct delivery created a Pick & Sort commissioning position"
