"""Seed, advance and observe a scratch MES4 world for the planner.

Scope is Pick & Sort commissioning (WP 1-5).  The world only contains what
the planner's queries read:

* ASRS20 boxes (``tblbufferpos`` ResourceId 17 + ``tblboxpos``),
* main orders with open feed steps (``tblorder``/``tblorderpos``/``tblstep``
  plus the part parameter the demand query reads via
  ``tbloperationparameter.ValueType = 3``),
* the commissioning sub-orders that the fake MES creates on 101/6 and the
  planner books.

:func:`advance` stands in for MES4 + Pick & Sort + Robotino + the station:
a fully booked commissioning order is executed in one step (part leaves the
source slot, the target box goes to the station, the feed step consumes the
part, the box comes back empty).  Timing is not modelled; the planner only
sees state.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .harness import ASRS_RESOURCE_ID, BOX_CAPACITY, BOX_TYPE_PNO, PICK_AND_SORT_RESOURCE_ID, MesDb

MAIN_WORKPLAN = 1000  # any WPNo < 6500 that is not a planner workplan
MAIN_STEP = 100
COMMISSIONING_WORKPLANS = (1, 2, 3, 4, 5)
DEFAULT_PARTS = (800, 801, 802, 810, 812, 814, 831, 832)


@dataclass(frozen=True)
class Resource:
    name: str
    resource_id: int
    workplan: int
    feed_op: int


def resources_from_config(config: dict[str, Any]) -> list[Resource]:
    """Commissioning resources (workplan > 0) from a planner config dict."""
    result = []
    for raw in config["resources"]:
        workplan = int(raw.get("workplanNumber", 0))
        feed_ops = [int(op) for op in raw.get("feedOperationNumbers", []) if int(op) > 0]
        if workplan > 0 and feed_ops:
            result.append(Resource(raw["name"], int(raw["resourceId"]), workplan, feed_ops[0]))
    return result


def part_parameter_no(db: MesDb, op_no: int) -> int:
    """The step parameter the demand query reads the part number from."""
    value = db.scalar(
        "SELECT MIN(ParameterNo) FROM tbloperationparameter WHERE OpNo = %s AND ValueType = 3",
        (op_no,),
    )
    if value is None:
        raise RuntimeError(f"op {op_no} has no ValueType-3 (part number) parameter")
    return int(value)


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _next_box_id(db: MesDb) -> int:
    return max(600, int(db.scalar("SELECT COALESCE(MAX(BoxId), 0) + 1 FROM tblboxpos", default=1)))


def _next_order_number(db: MesDb) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(ONo), 0) + 1 FROM tblorder", default=1))


def _free_asrs_positions(db: MesDb) -> list[int]:
    rows = db.query(
        "SELECT BufPos FROM tblbufferpos WHERE ResourceId = %s AND BufNo = 1 AND BoxID = 0 "
        "ORDER BY BufPos",
        (ASRS_RESOURCE_ID,),
    )
    return [int(row["BufPos"]) for row in rows]


def add_box(db: MesDb, slots: list[int]) -> int | None:
    """Store a box with the given slot contents (PNo per slot, 0 = empty).

    Returns the box id, or ``None`` when ASRS20 is full.
    """
    free = _free_asrs_positions(db)
    if not free:
        return None
    box_id = _next_box_id(db)
    for box_pos, part in enumerate(slots, start=1):
        db.execute(
            "INSERT INTO tblboxpos (BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
            "VALUES (%s, %s, %s, 0, 0, %s, 9)",
            (box_pos, BOX_TYPE_PNO, box_id, part),
        )
    db.execute(
        "UPDATE tblbufferpos SET PNo = %s, BoxID = %s, TimeStamp = NOW() "
        "WHERE ResourceId = %s AND BufNo = 1 AND BufPos = %s",
        (BOX_TYPE_PNO, box_id, ASRS_RESOURCE_ID, free[0]),
    )
    return box_id


def random_part_box(rng: random.Random, parts: list[int]) -> list[int]:
    """1-3 part types, 1-8 filled slots, the rest empty."""
    kinds = rng.sample(parts, k=min(len(parts), rng.randint(1, 3)))
    filled = rng.randint(1, BOX_CAPACITY)
    slots = [rng.choice(kinds) for _ in range(filled)] + [0] * (BOX_CAPACITY - filled)
    rng.shuffle(slots)
    return slots


def stock(db: MesDb) -> dict[int, int]:
    """Free (unbooked) parts per PNo in ASRS20."""
    rows = db.query(
        "SELECT b.PNo, COUNT(*) AS n FROM tblboxpos b "
        "JOIN tblbufferpos bp ON bp.BoxID = b.BoxId AND bp.ResourceId = %s "
        "WHERE b.PNo > 0 AND b.ONo = 0 AND b.OPos = 0 GROUP BY b.PNo",
        (ASRS_RESOURCE_ID,),
    )
    return {int(row["PNo"]): int(row["n"]) for row in rows}


def add_main_order(
    db: MesDb,
    resource: Resource,
    parts: list[int],
    *,
    planned_start: datetime | None = None,
) -> int:
    """One open main order with one feed step (position) per part."""
    order_number = _next_order_number(db)
    start = (planned_start or datetime.now()).replace(microsecond=0)
    parameter_no = part_parameter_no(db, resource.feed_op)
    db.execute(
        "INSERT INTO tblorder (ONo, PlannedStart, CNo, State, Enabled) VALUES (%s, %s, 0, 0, 1)",
        (order_number, start),
    )
    for opos, part in enumerate(parts, start=1):
        planned = start + timedelta(seconds=opos)
        db.execute(
            "INSERT INTO tblorderpos "
            "(ONo, OPos, PlannedStart, WPNo, StepNo, MainOPos, State, ResourceID, "
            " OpNo, PNo, OrderPNo) "
            "VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s)",
            (order_number, opos, planned, MAIN_WORKPLAN, MAIN_STEP, resource.resource_id,
             resource.feed_op, MAIN_WORKPLAN, MAIN_WORKPLAN),
        )
        db.execute(
            "INSERT INTO tblstep "
            "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
            " ResourceID, PlannedStart, Active) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, 1)",
            (MAIN_WORKPLAN, MAIN_STEP, order_number, opos, f"feed part for {resource.name}",
             resource.feed_op, resource.resource_id, planned),
        )
        db.execute(
            "INSERT INTO tblstepparameter "
            "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
            "VALUES (%s, %s, %s, %s, %s, %s, 2)",
            (MAIN_WORKPLAN, MAIN_STEP, order_number, opos, parameter_no, str(part)),
        )
    return order_number


def add_demands(
    db: MesDb,
    rng: random.Random,
    resources: list[Resource],
    parts: list[int],
    count: int,
    *,
    in_stock_bias: float = 0.9,
) -> list[int]:
    """``count`` feed demands, grouped into main orders of 1-4 positions.

    With probability ``in_stock_bias`` a part is drawn from what ASRS20
    currently holds (weighted by quantity), otherwise from ``parts``.
    """
    orders = []
    remaining = count
    while remaining > 0:
        size = min(remaining, rng.randint(1, 4))
        available = stock(db)
        chosen = []
        for _ in range(size):
            if available and rng.random() < in_stock_bias:
                part = rng.choices(list(available), weights=list(available.values()))[0]
            else:
                part = rng.choice(parts)
            chosen.append(part)
        orders.append(add_main_order(db, rng.choice(resources), chosen))
        remaining -= size
    return orders


def seed(
    db: MesDb,
    rng: random.Random,
    resources: list[Resource],
    parts: list[int],
    *,
    boxes: int,
    empty_boxes: int,
    demands: int,
) -> dict[str, int]:
    added = 0
    for _ in range(boxes):
        if add_box(db, random_part_box(rng, parts)) is not None:
            added += 1
    empty = 0
    for _ in range(empty_boxes):
        if add_box(db, [0] * BOX_CAPACITY) is not None:
            empty += 1
    orders = add_demands(db, rng, resources, parts, demands)
    return {"part_boxes": added, "empty_boxes": empty, "main_orders": len(orders), "demands": demands}


# ---------------------------------------------------------------------------
# MES effects between planner cycles
# ---------------------------------------------------------------------------


def commissioning_positions(db: MesDb, *, open_only: bool) -> list[dict[str, Any]]:
    """WP 1-5 StepNo-10 positions with parameters 3/4/6/7/8/9/10/11 pivoted."""
    rows = db.query(
        f"""
        SELECT op.ONo, op.OPos, op.WPNo, op.`End` IS NOT NULL AS ended,
               sp.ParameterNo, sp.Parameter
        FROM tblorderpos op
        JOIN tblstepparameter sp ON sp.ONo = op.ONo AND sp.OPos = op.OPos AND sp.StepNo = 10
        WHERE op.WPNo IN ({", ".join(["%s"] * len(COMMISSIONING_WORKPLANS))})
          AND sp.ParameterNo IN (3, 4, 6, 7, 8, 9, 10, 11)
          {"AND op.`End` IS NULL" if open_only else ""}
        ORDER BY op.ONo, op.OPos
        """,
        COMMISSIONING_WORKPLANS,
    )
    names = {3: "src_box", 4: "src_pos", 6: "tgt_box", 7: "tgt_pos",
             8: "main_ono", 9: "main_opos", 10: "main_step", 11: "part"}
    positions: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["ONo"]), int(row["OPos"]))
        pos = positions.setdefault(key, {"ono": key[0], "opos": key[1], "wp": int(row["WPNo"]),
                                         "ended": bool(row["ended"])})
        try:
            pos[names[int(row["ParameterNo"])]] = int(str(row["Parameter"]).strip() or 0)
        except ValueError:
            pos[names[int(row["ParameterNo"])]] = 0
    return list(positions.values())


def _finish_order_position(db: MesDb, ono: int, opos: int) -> None:
    db.execute(
        "UPDATE tblorderpos SET `End` = NOW(), State = 100 WHERE ONo = %s AND OPos = %s",
        (ono, opos),
    )
    db.execute("UPDATE tblstep SET `End` = NOW() WHERE ONo = %s AND OPos = %s", (ono, opos))


def _finish_order_if_done(db: MesDb, ono: int) -> None:
    open_positions = db.scalar(
        "SELECT COUNT(*) FROM tblorderpos WHERE ONo = %s AND `End` IS NULL", (ono,), default=0
    )
    if int(open_positions) == 0:
        db.execute(
            "UPDATE tblorder SET `End` = NOW(), State = 100, Enabled = 0 WHERE ONo = %s", (ono,)
        )


def advance(
    db: MesDb,
    rng: random.Random,
    parts: list[int],
    *,
    p_complete: float = 1.0,
    refill: bool = True,
    keep_empty_boxes: int = 4,
) -> dict[str, int]:
    """Apply MES/Pick & Sort/station effects to the planner's bookings.

    * A commissioning order whose positions all have a source (param 3/4)
      and a target slot (param 7) is executed with probability
      ``p_complete``: each part leaves its source slot, is delivered to the
      station and consumed by the main feed step; sub- and main positions
      end; the target box returns empty to ASRS20.
    * ``refill``: a part that the planner could not source gets a new ASRS20
      box holding it, so the next cycle can book it.
    * Fully empty boxes beyond ``keep_empty_boxes`` leave ASRS20, so the
      store does not fill up with empty boxes.
    """
    summary = {"orders_completed": 0, "parts_delivered": 0, "refill_boxes": 0, "boxes_removed": 0}
    by_order: dict[int, list[dict[str, Any]]] = {}
    for pos in commissioning_positions(db, open_only=True):
        by_order.setdefault(pos["ono"], []).append(pos)
    required = {"src_box", "src_pos", "tgt_box", "tgt_pos", "main_ono", "main_opos", "part"}

    for ono, positions in sorted(by_order.items()):
        if any(not required <= p.keys() for p in positions):
            continue  # MES4 has not finished creating the position's parameters
        unsourced = [p for p in positions if p.get("src_box", 0) <= 0]
        if unsourced:
            if refill:
                missing: dict[int, int] = {}
                for p in unsourced:
                    missing[p["part"]] = missing.get(p["part"], 0) + 1
                for part, n in missing.items():
                    if stock(db).get(part, 0) >= n:
                        continue  # stock exists; the planner books it next cycle
                    slots = [part] * min(BOX_CAPACITY, n + rng.randint(0, 4))
                    slots += [0] * (BOX_CAPACITY - len(slots))
                    if add_box(db, slots) is not None:
                        summary["refill_boxes"] += 1
            continue
        if any(p.get("tgt_pos", 0) <= 0 for p in positions):
            continue  # orphan; the planner's reconcile pass repairs it
        if rng.random() >= p_complete:
            continue

        for p in positions:
            # Pick & Sort op 220: the part leaves the source slot ...
            db.execute(
                "UPDATE tblboxpos SET PNo = 0, ONo = 0, OPos = 0 WHERE BoxId = %s AND BoxPos = %s",
                (p["src_box"], p["src_pos"]),
            )
            # ... op 215 re-books it to the main position, the feed step
            # consumes it and the box returns empty.
            db.execute(
                "UPDATE tblboxpos SET PNo = 0, ONo = 0, OPos = 0 WHERE BoxId = %s AND BoxPos = %s",
                (p["tgt_box"], p["tgt_pos"]),
            )
            _finish_order_position(db, p["ono"], p["opos"])
            _finish_order_position(db, p["main_ono"], p["main_opos"])
            _finish_order_if_done(db, p["main_ono"])
            summary["parts_delivered"] += 1
        _finish_order_if_done(db, ono)
        summary["orders_completed"] += 1

    summary["boxes_removed"] = _remove_surplus_empty_boxes(db, keep_empty_boxes)
    return summary


def _remove_surplus_empty_boxes(db: MesDb, keep: int) -> int:
    rows = db.query(
        "SELECT bp.BoxID FROM tblbufferpos bp JOIN tblboxpos b ON b.BoxId = bp.BoxID "
        "WHERE bp.ResourceId = %s AND bp.BoxID > 0 "
        "GROUP BY bp.BoxID, bp.BufPos "
        "HAVING SUM(b.PNo <> 0 OR b.ONo <> 0 OR b.OPos <> 0) = 0 "
        "ORDER BY bp.BufPos",
        (ASRS_RESOURCE_ID,),
    )
    surplus = [int(row["BoxID"]) for row in rows][keep:]
    for box_id in surplus:
        db.execute("DELETE FROM tblboxpos WHERE BoxId = %s", (box_id,))
        db.execute(
            "UPDATE tblbufferpos SET PNo = 0, BoxID = 0, TimeStamp = NOW() "
            "WHERE ResourceId = %s AND BoxID = %s",
            (ASRS_RESOURCE_ID, box_id),
        )
    return len(surplus)


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


def snapshot(db: MesDb) -> dict[str, Any]:
    """The state the planner reads, as plain JSON-able data."""
    boxes: dict[int, dict[str, Any]] = {}
    for row in db.query(
        "SELECT bp.ResourceId, bp.BufNo, bp.BufPos, b.BoxId, b.BoxPos, b.PNo, b.ONo, b.OPos "
        "FROM tblbufferpos bp JOIN tblboxpos b ON b.BoxId = bp.BoxID "
        "WHERE bp.BoxID > 0 ORDER BY bp.ResourceId, bp.BufNo, bp.BufPos, b.BoxPos"
    ):
        box = boxes.setdefault(int(row["BoxId"]), {
            "box_id": int(row["BoxId"]), "resource": int(row["ResourceId"]),
            "buf_no": int(row["BufNo"]), "buf_pos": int(row["BufPos"]), "slots": [],
        })
        box["slots"].append([int(row["BoxPos"]), int(row["PNo"]), int(row["ONo"]), int(row["OPos"])])

    buffers = [
        {k: int(v) for k, v in row.items()}
        for row in db.query(
            "SELECT ResourceId, BufNo, BufPos, PNo, BoxID, Booked FROM tblbufferpos "
            "WHERE ResourceId <> %s ORDER BY ResourceId, BufNo, BufPos",
            (ASRS_RESOURCE_ID,),
        )
    ]
    demands = [
        {"ono": int(r["ONo"]), "opos": int(r["OPos"]), "step": int(r["StepNo"]),
         "op": int(r["OpNo"]), "resource": int(r["ResourceID"]), "part": int(r["part"])}
        for r in db.query(
            "SELECT s.ONo, s.OPos, s.StepNo, s.OpNo, s.ResourceID, "
            "       CONVERT(sp.Parameter, UNSIGNED INTEGER) AS part "
            "FROM tblstep s "
            "JOIN tbloperationparameter o ON o.OpNo = s.OpNo AND o.ValueType = 3 "
            "JOIN tblstepparameter sp ON sp.ONo = s.ONo AND sp.OPos = s.OPos "
            "     AND sp.StepNo = s.StepNo AND sp.ParameterNo = o.ParameterNo "
            "WHERE s.WPNo = %s AND s.`End` IS NULL ORDER BY s.ONo, s.OPos",
            (MAIN_WORKPLAN,),
        )
    ]
    return {
        "boxes": list(boxes.values()),
        "buffers": buffers,
        "demands": demands,
        "commissioning": commissioning_positions(db, open_only=True),
    }


def booking_events(
    before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]],
    resource_by_wp: dict[int, str],
) -> list[dict[str, Any]]:
    """Planner decisions of one cycle, from the change in step-10 parameters.

    ``target``: a new commissioning position and the target slot booked for
    it.  ``source``: a source slot booked for a position (params 3/4 went
    from 0 to a slot).  A position created and sourced in the same cycle
    yields both, target first.
    """
    old = {(p["ono"], p["opos"]): p for p in before}
    events = []
    for p in after:
        key = (p["ono"], p["opos"])
        prev = old.get(key)
        common = {
            "sub": [p["ono"], p["opos"]], "wp": p["wp"], "resource": resource_by_wp.get(p["wp"]),
            "main": [p.get("main_ono", 0), p.get("main_opos", 0), p.get("main_step", 0)],
            "part": p.get("part", 0),
        }
        if prev is None or (prev.get("tgt_pos", 0) <= 0 < p.get("tgt_pos", 0)):
            events.append({"kind": "target", **common,
                           "box": p.get("tgt_box", 0), "pos": p.get("tgt_pos", 0)})
        if p.get("src_box", 0) > 0 and (prev is None or prev.get("src_box", 0) <= 0):
            events.append({"kind": "source", **common,
                           "box": p.get("src_box", 0), "pos": p.get("src_pos", 0)})
    return events


def check_invariants(db: MesDb) -> list[str]:
    """Problems that mean the generated data is not trustworthy."""
    problems = []
    for row in db.query(
        "SELECT BoxId, BoxPos, COUNT(*) AS n FROM tblboxpos GROUP BY BoxId, BoxPos HAVING n > 1"
    ):
        problems.append(f"duplicate slot row {row['BoxId']}/{row['BoxPos']}")
    seen: dict[tuple[int, int], tuple[int, int]] = {}
    for p in commissioning_positions(db, open_only=True):
        for kind in ("src", "tgt"):
            box, pos = p.get(f"{kind}_box", 0), p.get(f"{kind}_pos", 0)
            if box > 0 and pos > 0:
                other = seen.setdefault((box, pos), (p["ono"], p["opos"]))
                if other != (p["ono"], p["opos"]):
                    problems.append(f"slot {box}/{pos} booked by {other} and {(p['ono'], p['opos'])}")
    return problems
