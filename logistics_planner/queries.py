"""Repository of every MES4 query used by the planner.

One method per query from the TypeScript ``src/utils/mes4.ts``; the TS line
references are in each docstring.  Values are always bound with ``%s``; only
table/column names and a few structural fragments are part of the SQL text.

The two TypeScript helpers that looked up the active commissioning order in
separate queries (``whichBoxHasCommissioningOrderForResource`` and
``whichIsTheCommissioiningOrderForResource``) are replaced by
:meth:`Repository.active_commissioning_order`, which returns ONo and box id
together and refuses to guess when the database reports more than one box.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Config, ResourceConfig
from .db import Db
from .decisions import SourceSlotCandidate
from .errors import (
    CapacityError,
    ConcurrentWriteError,
    DatabaseError,
    PlanningError,
    PlannerError,
    QueryRowcountError,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartDemand:
    order_number: int
    order_position: int
    step_number: int
    part_number: int


@dataclass(frozen=True)
class ActiveCommissioningOrder:
    order_number: int
    box_id: int


@dataclass(frozen=True)
class DirectDemandGroup:
    """All open feed parts of one main order position at one station."""

    order_number: int
    order_position: int
    parts: tuple[PartDemand, ...]


@dataclass(frozen=True)
class BoxSlot:
    box_id: int
    box_position: int
    part_number: int


@dataclass(frozen=True)
class OrderRef:
    order_number: int
    order_position: int


@dataclass(frozen=True)
class PartRequest:
    part_number: int
    quantity: int
    orders: list[OrderRef]


@dataclass(frozen=True)
class BoxPosition:
    box_id: int
    box_position: int


@dataclass(frozen=True)
class DisassemblyRequest:
    order_number: int
    order_position: int


@dataclass(frozen=True)
class UnbookedPosition:
    order_number: int
    order_position: int
    box_id: int
    target_position: str | None
    existing_box_position: int | None


@dataclass(frozen=True)
class StaleSourceBooking:
    """A not-yet-started step-10 position whose booked source slot is invalid.

    ``slot_*`` describe what the slot named by parameters 3/4 holds now; they
    are ``None`` when that slot does not exist (e.g. parameter 4 is 0).
    """

    order_number: int
    order_position: int
    box_id: int
    box_position: int
    part_number: int
    slot_order_number: int | None
    slot_order_position: int | None
    slot_part_number: int | None

    @property
    def slot_still_reserved(self) -> bool:
        return (
            self.slot_order_number == self.order_number
            and self.slot_order_position == self.order_position
        )


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _dedupe(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    result: list[tuple[int, int]] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class Repository:
    def __init__(self, db: Db, config: Config) -> None:
        self._db = db
        self._config = config
        # Read-only dry-run never writes, so later reads in a cycle cannot see
        # earlier "bookings".  These sets stand in for those writes within one
        # cycle (reset by begin_read_only_cycle) so the shadow log books each
        # part into a distinct slot, as the live planner would.
        self._shadow_target_slots: dict[int, set[int]] = {}
        self._shadow_source_slots: set[tuple[int, int]] = set()

    # ------------------------------------------------------------ transaction

    @property
    def dry_run(self) -> bool:
        """True for read-only shadow mode (no DB writes, no MES TCP)."""
        return self._db.dry_run

    @property
    def read_only(self) -> bool:
        return self._db.dry_run

    @property
    def simulate(self) -> bool:
        """True for the copy-DB simulation mode with rolled-back MES rows."""
        return self._db.simulate

    def transaction(self):
        return self._db.transaction()

    def ensure_connected(self) -> None:
        self._db.ensure_connected()

    def begin_read_only_cycle(self) -> None:
        """Forget the previous dry-run cycle's simulated slot bookings."""
        self._shadow_target_slots.clear()
        self._shadow_source_slots.clear()

    def _shadow_reserved(self, box_id: int) -> set[int]:
        return self._shadow_target_slots.get(box_id, set()) if self._db.dry_run else set()

    def begin_simulate_cycle(self) -> None:
        self._db.begin_simulate_cycle()

    def end_simulate_cycle(self) -> None:
        self._db.end_simulate_cycle()

    # Backwards-compatible names used by older callers/tests.
    def begin_dry_run_cycle(self) -> None:
        self.begin_simulate_cycle()

    def end_dry_run_cycle(self) -> None:
        self.end_simulate_cycle()

    # --------------------------------------------------------------- buffers

    def resource_buffer_has_empty_place(self, resource: ResourceConfig) -> bool:
        """mes4.ts:5-19.

        The ``BufNo = 1`` filter is intentionally kept commented out (as in
        the TypeScript source): the query counts any free box at the resource
        with ``BufPos = 2``.
        """
        rows = self._db.query(
            """
            SELECT
                Sum(tblbufferpos.PNo = 0) AS noBox
            FROM
                tblbufferpos
            WHERE
                tblbufferpos.ResourceId = %s AND
                /*tblbufferpos.BufNo = 1 AND*/
                tblbufferpos.BufPos = 2
            """,
            (resource.resource_id,),
        )
        return bool(rows) and _int_or_zero(rows[0].get("noBox")) > 0

    def resource_buffer_fully_empty_place(self, resource: ResourceConfig) -> bool:
        """mes4.ts:21-35 (kept for completeness; the planner policy uses the
        ``BufPos = 2`` variant above)."""
        rows = self._db.query(
            """
            SELECT
                Sum(tblbufferpos.PNo = 0) AS noBox
            FROM
                tblbufferpos
            WHERE
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BufNo = 1
                /*tblbufferpos.BufPos = 2*/
            """,
            (resource.resource_id,),
        )
        return bool(rows) and _int_or_zero(rows[0].get("noBox")) > 0

    def there_is_a_commissioned_box_at_resource(self, resource: ResourceConfig) -> bool:
        """mes4.ts:37-60 (feed operations, any order position)."""
        placeholders = ", ".join(["%s"] * len(resource.feed_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT
                Count(tblorderpos.OPos) > 0 AS has
            FROM
                tblorderpos
                LEFT JOIN tblboxpos ON tblorderpos.ONo = tblboxpos.ONo AND tblorderpos.OPos = tblboxpos.OPos
            WHERE
                tblboxpos.BoxPos > 0 AND
                tblorderpos.OpNo IN ({placeholders})
            """,
            resource.feed_operation_numbers,
        )
        return bool(rows) and bool(rows[0].get("has"))

    def there_is_a_commissioned_box_at_resource_for_disassembly(
        self, resource: ResourceConfig
    ) -> bool:
        """mes4.ts:62-85 (remove operations)."""
        placeholders = ", ".join(["%s"] * len(resource.remove_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT
                Count(tblorderpos.OPos) > 0 AS has
            FROM
                tblorderpos
                LEFT JOIN tblboxpos ON tblorderpos.ONo = tblboxpos.ONo AND tblorderpos.OPos = tblboxpos.OPos
            WHERE
                tblboxpos.BoxPos > 0 AND
                tblorderpos.OpNo IN ({placeholders})
            """,
            resource.remove_operation_numbers,
        )
        return bool(rows) and bool(rows[0].get("has"))

    def there_is_a_commissioning_order_for_resource(self, resource: ResourceConfig) -> bool:
        """mes4.ts:88-125, restricted to a still-open position.

        The TypeScript query only checked ``tblorder.Enabled``.  A finished
        order can stay enabled until MES4 moves it to ``tblfinorder``; counting
        its ended positions would keep the planner from ever starting the next
        order.  ``End IS NULL`` makes this agree with
        :meth:`active_commissioning_order`.
        """
        rows = self._db.query(
            """
            SELECT
                Count(tblorderpos.OPos) > 0 AS has
            FROM
                tblorderpos
                INNER JOIN tblorder ON
                    tblorderpos.ONo = tblorder.ONo
            WHERE
                tblorderpos.WPNo = %s AND
                tblorderpos.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1
            """,
            (resource.workplan_number,),
        )
        return bool(rows) and bool(rows[0].get("has"))

    def there_is_a_disassembly_order_for_resource(self, resource: ResourceConfig) -> bool:
        """mes4.ts:127-164, restricted to a still-open position."""
        rows = self._db.query(
            """
            SELECT
                Count(tblorderpos.OPos) > 0 AS has
            FROM
                tblorderpos
                INNER JOIN tblorder ON
                    tblorderpos.ONo = tblorder.ONo
            WHERE
                tblorderpos.WPNo = %s AND
                tblorderpos.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1
            """,
            (resource.workplan_disassembly_number,),
        )
        return bool(rows) and bool(rows[0].get("has"))

    def there_is_an_open_store_order_for_resource(self, resource: ResourceConfig) -> bool:
        """True while a WP 6–9 store box is on the way to (or at) ``resource``.

        Direct delivery creates the store order in the same ``#String``
        (``<wpDis>-<mainONo>-<mainOPos>``) as disassembly; its StepNo-10 op 215
        stores the box at the station.  The guard therefore reuses the
        disassembly-order check.
        """
        return self.there_is_a_disassembly_order_for_resource(resource)

    def active_commissioning_order(
        self, resource: ResourceConfig
    ) -> ActiveCommissioningOrder | None:
        """Return the active (ONo, target box) pair for ``resource``.

        Replaces mes4.ts:167-205.  Only positions with ``End IS NULL`` and an
        enabled order count as active.  Parameter 6 is the target box id and
        parameter 8 is the main order number (the link that makes a position a
        commissioning position).

        If the database reports positions in more than one box, the state is
        ambiguous and a :class:`PlanningError` is raised instead of guessing.
        """
        rows = self._db.query(
            """
            SELECT DISTINCT
                tblorderpos.ONo AS orderNumber,
                CONVERT(box.Parameter, UNSIGNED INTEGER) AS boxId
            FROM
                tblorderpos
                INNER JOIN tblorder ON tblorderpos.ONo = tblorder.ONo
                INNER JOIN tblstepparameter box ON
                    tblorderpos.ONo = box.ONo AND
                    tblorderpos.OPos = box.OPos AND
                    box.StepNo = 10 AND
                    box.ParameterNo = 6
                INNER JOIN tblstepparameter main ON
                    tblorderpos.ONo = main.ONo AND
                    tblorderpos.OPos = main.OPos AND
                    main.StepNo = 10 AND
                    main.ParameterNo = 8
            WHERE
                tblorderpos.WPNo = %s AND
                tblorderpos.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1
            ORDER BY
                tblorderpos.ONo DESC
            """,
            (resource.workplan_number,),
        )
        if not rows:
            return None

        order_number = int(rows[0]["orderNumber"])
        order_numbers = sorted({int(row["orderNumber"]) for row in rows})
        boxes = sorted({_int_or_zero(row.get("boxId")) for row in rows})
        if len(order_numbers) > 1:
            raise PlanningError(
                f"{resource.name}: more than one commissioning order is active "
                f"({', '.join(str(number) for number in order_numbers)}); refusing to guess"
            )
        if len(boxes) > 1:
            raise PlanningError(
                f"{resource.name}: commissioning order {order_number} is active in more than "
                f"one box ({', '.join(str(box) for box in boxes)}); refusing to guess"
            )
        box_id = boxes[0]
        if box_id <= 0:
            raise PlanningError(
                f"{resource.name}: commissioning order {order_number} has an unknown target box"
            )
        return ActiveCommissioningOrder(order_number=order_number, box_id=box_id)

    # ------------------------------------------------------------- target box

    def box_has_empty_place(self, box_id: int) -> bool:
        """mes4.ts:207-223."""
        rows = self._db.query(
            """
            SELECT
                tblboxpos.BoxPos as boxPosition
            FROM
                tblboxpos
            WHERE
                tblboxpos.BoxPNo = %s AND
                tblboxpos.BoxId = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = 0
            """,
            (self._config.box.type_pno, box_id),
        )
        reserved = self._shadow_reserved(box_id)
        return any(int(row["boxPosition"]) not in reserved for row in rows)

    def count_free_box_slots(self, box_id: int) -> int:
        """Number of free slots in ``box_id`` (capacity fix, see commissioning.py)."""
        rows = self._db.query(
            """
            SELECT
                Count(*) AS freeSlots
            FROM
                tblboxpos
            WHERE
                tblboxpos.BoxPNo = %s AND
                tblboxpos.BoxId = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = 0
            """,
            (self._config.box.type_pno, box_id),
        )
        free = _int_or_zero(rows[0].get("freeSlots")) if rows else 0
        return max(0, free - len(self._shadow_reserved(box_id)))

    def which_box_to_use_for_new_commissioning_orders(self, number_of_free_positions: int) -> int:
        """mes4.ts:239-272 (an ASRS20 box with exactly N free slots)."""
        rows = self._db.query(
            f"""
            SELECT
                tblboxpos.BoxId as boxId
            FROM
                tblbufferpos
                INNER JOIN tblboxpos ON
                    tblbufferpos.PNo = tblboxpos.BoxPNo AND
                    tblbufferpos.BoxID = tblboxpos.BoxId
            WHERE
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BufNo = 1 AND
                tblboxpos.BoxPNo = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = 0
            GROUP BY
                tblboxpos.BoxId,
                tblbufferpos.BufNo,
                tblbufferpos.BufPos
            HAVING
                Count(tblboxpos.BoxPos) = %s
            ORDER BY
                tblbufferpos.BufPos
            {"" if self._db.dry_run else "LIMIT 1"}
            """,
            (
                self._config.asrs_resource_id,
                self._config.box.type_pno,
                number_of_free_positions,
            ),
        )
        for row in rows:
            box_id = int(row["boxId"])
            if not self._shadow_reserved(box_id):
                return box_id
        return 0

    def reserve_free_box_slot(self, box_id: int) -> int:
        """Return the lowest free slot of ``box_id``.

        In live/simulate mode this must run inside a transaction and locks the
        row with ``FOR UPDATE`` until commit/rollback.  Read-only shadow mode
        uses a plain SELECT; ``Db.execute`` discards the later writes.
        """
        if not self._db.in_transaction and not self._db.dry_run:
            raise PlannerError("reserve_free_box_slot must run inside a transaction")
        lock_clause = "" if self._db.dry_run else "\n            FOR UPDATE"
        # Dry-run reads every free slot and skips the ones it "booked" earlier
        # in this cycle; live/simulate locks exactly the lowest one.
        limit_clause = "" if self._db.dry_run else "LIMIT 1"
        rows = self._db.query(
            f"""
            SELECT
                tblboxpos.BoxPos AS boxPosition
            FROM
                tblboxpos
            WHERE
                tblboxpos.BoxPNo = %s AND
                tblboxpos.BoxId = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = 0
            ORDER BY
                tblboxpos.BoxPos
            {limit_clause}{lock_clause}
            """,
            (self._config.box.type_pno, box_id),
        )
        reserved = self._shadow_reserved(box_id)
        rows = [row for row in rows if int(row["boxPosition"]) not in reserved]
        if not rows:
            raise CapacityError(f"box {box_id} has no free slot")
        box_position = int(rows[0]["boxPosition"])
        if box_position <= 0:
            raise CapacityError(f"box {box_id} returned invalid free position {box_position}")
        if self._db.dry_run:
            self._shadow_target_slots.setdefault(box_id, set()).add(box_position)
        return box_position

    def book_reserved_box_slot(
        self, box_id: int, box_position: int, order_number: int, order_position: int
    ) -> None:
        """Guarded write of the exact slot reserved by ``reserve_free_box_slot``."""
        self._rowcount_one(
            """
            UPDATE
                tblboxpos
            SET
                tblboxpos.ONo = %s,
                tblboxpos.OPos = %s
            WHERE
                tblboxpos.BoxId = %s AND
                tblboxpos.BoxPos = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = 0
            """,
            (order_number, order_position, box_id, box_position),
            f"booking box {box_id}.{box_position} to order {order_number}.{order_position}",
            concurrent=True,
        )

    def book_box_pos_as_target_box_pos_to_order(
        self, order_number: int, order_position: int, box_position: int
    ) -> None:
        """mes4.ts:946-966 (ParameterNo 7).  Writing 0 is never allowed."""
        if box_position <= 0:
            raise CapacityError(f"refusing to write ParameterNo 7 = {box_position}")
        self._rowcount_one(
            """
            UPDATE
                tblstepparameter
            SET
                tblstepparameter.Parameter = %s
            WHERE
                tblstepparameter.ONo = %s AND
                tblstepparameter.OPos = %s AND
                tblstepparameter.StepNo = 10 AND
                tblstepparameter.ParameterNo = 7
            """,
            (str(box_position), order_number, order_position),
            f"writing target box position {box_position} to order {order_number}.{order_position}",
        )

    # --------------------------------------------------------------- planning

    def which_parts_to_commission_for_resource(
        self, resource: ResourceConfig, limit: int
    ) -> list[PartDemand]:
        """mes4.ts:274-364, with ``LIMIT 8`` replaced by the configured capacity."""
        placeholders = ", ".join(["%s"] * len(resource.feed_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT
                tblstep.ONo AS orderNumber,
                tblstep.OPos AS orderPosition,
                tblstep.StepNo AS stepNumber,
                CONVERT(tblstepparameter.Parameter, UNSIGNED INTEGER) AS partNumber
            FROM
                tblstep
                INNER JOIN tblstepparameter ON
                    tblstep.StepNo = tblstepparameter.StepNo AND
                    tblstep.ONo = tblstepparameter.ONo AND
                    tblstep.OPos = tblstepparameter.OPos
                INNER JOIN tbloperationparameter ON
                    tblstep.OpNo = tbloperationparameter.OpNo AND
                    tblstepparameter.ParameterNo = tbloperationparameter.ParameterNo
                INNER JOIN tblorder ON
                    tblstep.ONo = tblorder.ONo
                INNER JOIN tblorderpos ON
                    tblstep.ONo = tblorderpos.ONo AND
                    tblstep.OPos = tblorderpos.OPos
            WHERE
                tblstep.OpNo IN ({placeholders}) AND
                tblstepparameter.Parameter > 0 AND
                tblorderpos.`End` IS NULL AND
                tblstep.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1 AND

                /* Exclude workplans >= 6500 (Mixed Production). */
                tblstep.WPNo < 6500 AND

                /* Only "part number" parameters. */
                tbloperationparameter.ValueType = 3 AND

                /* Exclude demands that already have a commissioning position. */
                Concat(tblstep.ONo, ".", tblstep.OPos, ".", tblstep.StepNo) NOT IN (
                    SELECT
                        Concat(
                            tblstepparameterONo.Parameter, ".",
                            tblstepparameterOPos.Parameter, ".",
                            tblstepparameterStepNo.Parameter
                        ) AS id
                    FROM
                        tblstepparameter tblstepparameterONo
                        INNER JOIN tblstepparameter tblstepparameterOPos ON
                            tblstepparameterONo.StepNo = tblstepparameterOPos.StepNo AND
                            tblstepparameterONo.ONo = tblstepparameterOPos.ONo AND
                            tblstepparameterONo.OPos = tblstepparameterOPos.OPos AND
                            tblstepparameterONo.WPNo = tblstepparameterOPos.WPNo
                        INNER JOIN tblstepparameter tblstepparameterStepNo ON
                            tblstepparameterOPos.StepNo = tblstepparameterStepNo.StepNo AND
                            tblstepparameterOPos.ONo = tblstepparameterStepNo.ONo AND
                            tblstepparameterOPos.OPos = tblstepparameterStepNo.OPos AND
                            tblstepparameterOPos.WPNo = tblstepparameterStepNo.WPNo
                    WHERE
                        tblstepparameterONo.ParameterNo = 8 AND /* main ONo */
                        tblstepparameterOPos.ParameterNo = 9 AND /* main OPos */
                        tblstepparameterStepNo.ParameterNo = 10 AND /* main StepNo */
                        tblstepparameterONo.StepNo = 10
                )
            ORDER BY
                orderNumber, orderPosition, stepNumber
            LIMIT %s
            """,
            (*resource.feed_operation_numbers, limit),
        )
        return [
            PartDemand(
                order_number=int(row["orderNumber"]),
                order_position=int(row["orderPosition"]),
                step_number=int(row["stepNumber"]),
                part_number=int(row["partNumber"]),
            )
            for row in rows
        ]

    def direct_delivery_demands(
        self, resource: ResourceConfig
    ) -> list[DirectDemandGroup]:
        """Open feed demands of one station, grouped by main order position.

        Same filters as :meth:`which_parts_to_commission_for_resource` (no
        ``LIMIT``), plus the direct-delivery exclusions: a main position that
        already has a step-10 commissioning link, an already-booked box slot,
        or an open store (WP 6–9) sub-order must never be split between
        Pick & Sort and direct delivery.
        """
        placeholders = ", ".join(["%s"] * len(resource.feed_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT
                tblstep.ONo AS orderNumber,
                tblstep.OPos AS orderPosition,
                tblstep.StepNo AS stepNumber,
                CONVERT(tblstepparameter.Parameter, UNSIGNED INTEGER) AS partNumber
            FROM
                tblstep
                INNER JOIN tblstepparameter ON
                    tblstep.StepNo = tblstepparameter.StepNo AND
                    tblstep.ONo = tblstepparameter.ONo AND
                    tblstep.OPos = tblstepparameter.OPos
                INNER JOIN tbloperationparameter ON
                    tblstep.OpNo = tbloperationparameter.OpNo AND
                    tblstepparameter.ParameterNo = tbloperationparameter.ParameterNo
                INNER JOIN tblorder ON
                    tblstep.ONo = tblorder.ONo
                INNER JOIN tblorderpos ON
                    tblstep.ONo = tblorderpos.ONo AND
                    tblstep.OPos = tblorderpos.OPos
            WHERE
                tblstep.OpNo IN ({placeholders}) AND
                tblstepparameter.Parameter > 0 AND
                tblorderpos.`End` IS NULL AND
                tblstep.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1 AND

                /* Exclude workplans >= 6500 (Mixed Production). */
                tblstep.WPNo < 6500 AND

                /* Only "part number" parameters. */
                tbloperationparameter.ValueType = 3 AND

                /*
                 * A main position is never split: no existing step-10
                 * commissioning sub-order may reference it (params 8/9).
                 */
                Concat(tblstep.ONo, ".", tblstep.OPos) NOT IN (
                    SELECT
                        Concat(main_ono.Parameter, ".", main_opos.Parameter)
                    FROM
                        tblstepparameter main_ono
                        INNER JOIN tblstepparameter main_opos ON
                            main_ono.StepNo = main_opos.StepNo AND
                            main_ono.ONo = main_opos.ONo AND
                            main_ono.OPos = main_opos.OPos AND
                            main_ono.WPNo = main_opos.WPNo
                    WHERE
                        main_ono.ParameterNo = 8 AND /* main ONo */
                        main_opos.ParameterNo = 9 AND /* main OPos */
                        main_ono.StepNo = 10
                ) AND

                /* A box slot may not already be booked to the main position. */
                NOT EXISTS (
                    SELECT 1
                    FROM tblboxpos booked_slot
                    WHERE
                        booked_slot.ONo = tblstep.ONo AND
                        booked_slot.OPos = tblstep.OPos
                ) AND

                /*
                 * An open WP 6–9 store sub-order records the main position in
                 * its StepNo-10 parameters 3/4.  It is on its way already.
                 */
                NOT EXISTS (
                    SELECT 1
                    FROM tblorderpos store_order_pos
                    INNER JOIN tblorder store_order ON
                        store_order.ONo = store_order_pos.ONo
                    INNER JOIN tblstepparameter main_ono ON
                        main_ono.ONo = store_order_pos.ONo AND
                        main_ono.OPos = store_order_pos.OPos AND
                        main_ono.StepNo = store_order_pos.StepNo AND
                        main_ono.ParameterNo = 3
                    INNER JOIN tblstepparameter main_opos ON
                        main_opos.ONo = main_ono.ONo AND
                        main_opos.OPos = main_ono.OPos AND
                        main_opos.StepNo = main_ono.StepNo AND
                        main_opos.ParameterNo = 4
                    WHERE
                        store_order_pos.WPNo = %s AND
                        store_order_pos.StepNo = 10 AND
                        store_order_pos.`End` IS NULL AND
                        store_order.Enabled = 1 AND
                        store_order.`End` IS NULL AND
                        CONVERT(main_ono.Parameter, UNSIGNED INTEGER) = tblstep.ONo AND
                        CONVERT(main_opos.Parameter, UNSIGNED INTEGER) = tblstep.OPos
                )
            ORDER BY
                orderNumber, orderPosition, stepNumber
            """,
            (*resource.feed_operation_numbers, resource.workplan_disassembly_number),
        )

        grouped: dict[tuple[int, int], list[PartDemand]] = {}
        for row in rows:
            demand = PartDemand(
                order_number=int(row["orderNumber"]),
                order_position=int(row["orderPosition"]),
                step_number=int(row["stepNumber"]),
                part_number=int(row["partNumber"]),
            )
            grouped.setdefault(
                (demand.order_number, demand.order_position), []
            ).append(demand)
        return [
            DirectDemandGroup(
                order_number=order_number,
                order_position=order_position,
                parts=tuple(parts),
            )
            for (order_number, order_position), parts in grouped.items()
        ]

    def unbooked_commissioning_positions(self) -> list[UnbookedPosition]:
        """Open WP 1-5 step-10 positions with no usable target slot.

        This is the repair query used at the start of every plan cycle.  It
        catches both the old TypeScript param-7=0 orphans and positions left
        behind when MES 101/6 committed but the local booking failed.
        """
        rows = self._db.query(
            """
            SELECT
                op.ONo AS orderNumber,
                op.OPos AS orderPosition,
                CONVERT(MAX(p6.Parameter), UNSIGNED INTEGER) AS boxId,
                MAX(p7.Parameter) AS targetPosition,
                MIN(box.BoxPos) AS existingBoxPos
            FROM
                tblorderpos op
                INNER JOIN tblorder o ON op.ONo = o.ONo
                LEFT JOIN tblstepparameter p6 ON
                    op.ONo = p6.ONo AND
                    op.OPos = p6.OPos AND
                    p6.StepNo = 10 AND
                    p6.ParameterNo = 6
                LEFT JOIN tblstepparameter p7 ON
                    op.ONo = p7.ONo AND
                    op.OPos = p7.OPos AND
                    p7.StepNo = 10 AND
                    p7.ParameterNo = 7
                LEFT JOIN tblboxpos box ON
                    box.ONo = op.ONo AND
                    box.OPos = op.OPos AND
                    box.BoxId = CONVERT(p6.Parameter, UNSIGNED INTEGER) AND
                    box.BoxPNo = %s
            WHERE
                op.WPNo BETWEEN 1 AND 5 AND
                op.StepNo = 10 AND
                op.OpNo = 220 AND
                op.`End` IS NULL AND
                o.Enabled = 1 AND
                o.`End` IS NULL
            GROUP BY
                op.ONo,
                op.OPos
            HAVING
                MAX(p7.Parameter) IS NULL OR
                CAST(MAX(p7.Parameter) AS UNSIGNED) = 0 OR
                MIN(box.BoxPos) IS NULL
            ORDER BY
                op.ONo,
                op.OPos
            """,
            (self._config.box.type_pno,),
        )
        result: list[UnbookedPosition] = []
        for row in rows:
            existing = row.get("existingBoxPos")
            result.append(
                UnbookedPosition(
                    order_number=int(row["orderNumber"]),
                    order_position=int(row["orderPosition"]),
                    box_id=_int_or_zero(row.get("boxId")),
                    target_position=(
                        None
                        if row.get("targetPosition") is None
                        else str(row["targetPosition"])
                    ),
                    existing_box_position=(
                        None if existing is None else int(existing)
                    ),
                )
            )
        return result

    def stale_source_bookings(self) -> list[StaleSourceBooking]:
        """Booked source slots that no longer hold the part for their position.

        Covers a source slot that was overwritten after booking (e.g. a box
        refilled with another part in the MES UI), whose reservation was taken
        by another order, or a half-written booking (parameter 4 = 0).  Without
        this check the step never starts: MES cannot resolve the source buffer
        while the box stays in the AS/RS, and the source booking only serves
        positions whose parameter 3 is still 0.

        Only positions that have not started are returned (``State = 0``,
        ``Start IS NULL``) and whose target slot is still empty, so a step that
        is executing or whose op-end SQL already moved the part is never
        touched.
        """
        rows = self._db.query(
            """
            SELECT
                op.ONo AS orderNumber,
                op.OPos AS orderPosition,
                CONVERT(p3.Parameter, UNSIGNED INTEGER) AS boxId,
                CONVERT(p4.Parameter, UNSIGNED INTEGER) AS boxPosition,
                CONVERT(p11.Parameter, UNSIGNED INTEGER) AS partNumber,
                src.ONo AS slotOrderNumber,
                src.OPos AS slotOrderPosition,
                src.PNo AS slotPartNumber
            FROM
                tblorderpos op
                INNER JOIN tblorder o ON op.ONo = o.ONo
                INNER JOIN tblstepparameter p3 ON
                    op.ONo = p3.ONo AND op.OPos = p3.OPos AND
                    p3.StepNo = 10 AND p3.ParameterNo = 3
                INNER JOIN tblstepparameter p4 ON
                    op.ONo = p4.ONo AND op.OPos = p4.OPos AND
                    p4.StepNo = 10 AND p4.ParameterNo = 4
                INNER JOIN tblstepparameter p11 ON
                    op.ONo = p11.ONo AND op.OPos = p11.OPos AND
                    p11.StepNo = 10 AND p11.ParameterNo = 11
                LEFT JOIN tblstepparameter p6 ON
                    op.ONo = p6.ONo AND op.OPos = p6.OPos AND
                    p6.StepNo = 10 AND p6.ParameterNo = 6
                LEFT JOIN tblstepparameter p7 ON
                    op.ONo = p7.ONo AND op.OPos = p7.OPos AND
                    p7.StepNo = 10 AND p7.ParameterNo = 7
                LEFT JOIN tblboxpos src ON
                    src.BoxId = CONVERT(p3.Parameter, UNSIGNED INTEGER) AND
                    src.BoxPos = CONVERT(p4.Parameter, UNSIGNED INTEGER) AND
                    src.BoxPNo = %s
                LEFT JOIN tblboxpos tgt ON
                    tgt.BoxId = CONVERT(p6.Parameter, UNSIGNED INTEGER) AND
                    tgt.BoxPos = CONVERT(p7.Parameter, UNSIGNED INTEGER) AND
                    tgt.BoxPNo = %s
            WHERE
                op.WPNo BETWEEN 1 AND 5 AND
                op.StepNo = 10 AND
                op.OpNo = 220 AND
                op.State = 0 AND
                op.Start IS NULL AND
                op.`End` IS NULL AND
                o.Enabled = 1 AND
                o.`End` IS NULL AND
                CONVERT(p3.Parameter, UNSIGNED INTEGER) > 0 AND
                COALESCE(tgt.PNo, 0) = 0 AND
                (
                    src.BoxPos IS NULL OR
                    src.ONo <> op.ONo OR
                    src.OPos <> op.OPos OR
                    src.PNo <> CONVERT(p11.Parameter, UNSIGNED INTEGER)
                )
            ORDER BY
                op.ONo,
                op.OPos
            """,
            (self._config.box.type_pno, self._config.box.type_pno),
        )
        return [
            StaleSourceBooking(
                order_number=int(row["orderNumber"]),
                order_position=int(row["orderPosition"]),
                box_id=_int_or_zero(row.get("boxId")),
                box_position=_int_or_zero(row.get("boxPosition")),
                part_number=_int_or_zero(row.get("partNumber")),
                slot_order_number=_int_or_none(row.get("slotOrderNumber")),
                slot_order_position=_int_or_none(row.get("slotOrderPosition")),
                slot_part_number=_int_or_none(row.get("slotPartNumber")),
            )
            for row in rows
        ]

    def release_source_slot(
        self, box_id: int, box_position: int, order_number: int, order_position: int
    ) -> None:
        """Clear a source-slot reservation that still points at this position."""
        self._rowcount_one(
            """
            UPDATE
                tblboxpos
            SET
                tblboxpos.ONo = 0,
                tblboxpos.OPos = 0
            WHERE
                tblboxpos.BoxId = %s AND
                tblboxpos.BoxPos = %s AND
                tblboxpos.ONo = %s AND
                tblboxpos.OPos = %s
            """,
            (box_id, box_position, order_number, order_position),
            f"releasing source slot {box_id}.{box_position} of order "
            f"{order_number}.{order_position}",
            concurrent=True,
        )

    def reset_source_booking(
        self, order_number: int, order_position: int, box_id: int, box_position: int
    ) -> None:
        """Set parameters 3/4 back to 0, guarded by the values that were read."""
        for parameter_number, expected in ((3, box_id), (4, box_position)):
            self._rowcount_one(
                """
                UPDATE
                    tblstepparameter
                SET
                    tblstepparameter.Parameter = '0'
                WHERE
                    tblstepparameter.ONo = %s AND
                    tblstepparameter.OPos = %s AND
                    tblstepparameter.StepNo = 10 AND
                    tblstepparameter.ParameterNo = %s AND
                    CONVERT(tblstepparameter.Parameter, UNSIGNED INTEGER) = %s
                """,
                (order_number, order_position, parameter_number, expected),
                f"resetting source parameter {parameter_number} ({expected}) of order "
                f"{order_number}.{order_position}",
                concurrent=True,
            )

    # ------------------------------------------------------------ disassembly

    def which_disassembly_order_needs_empty_box(
        self, resource: ResourceConfig, limit: int
    ) -> list[DisassemblyRequest]:
        """mes4.ts:366-406.

        The accidental ``GROUP BY PlannedStart, StepNo`` is replaced by a
        plain ``ORDER BY PlannedStart, ONo, OPos``.  ``SELECT DISTINCT``
        removes the duplicate rows produced by the unused
        ``LEFT JOIN tblboxpos`` without the non-aggregated ``GROUP BY``.
        """
        placeholders = ", ".join(["%s"] * len(resource.remove_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT DISTINCT
                tblstep.ONo AS orderNumber,
                tblstep.OPos AS orderPosition
            FROM
                tblstep
                LEFT JOIN tblboxpos ON tblstep.ONo = tblboxpos.ONo AND tblstep.OPos = tblboxpos.OPos
                INNER JOIN tblorderpos ON
                    tblstep.ONo = tblorderpos.ONo AND
                    tblstep.OPos = tblorderpos.OPos
                INNER JOIN tblorder ON
                    tblorderpos.ONo = tblorder.ONo
            WHERE
                tblstep.OpNo IN ({placeholders}) AND
                tblorderpos.`End` IS NULL AND
                tblstep.`End` IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1
            ORDER BY
                tblstep.PlannedStart,
                tblstep.ONo,
                tblstep.OPos
            LIMIT %s
            """,
            (*resource.remove_operation_numbers, limit),
        )
        return [
            DisassemblyRequest(int(order), int(position))
            for order, position in _dedupe(
                [(int(row["orderNumber"]), int(row["orderPosition"])) for row in rows]
            )
        ]

    def which_disassembly_order_needs_empty_box_without_store(
        self, resource: ResourceConfig, limit: int
    ) -> list[DisassemblyRequest]:
        """mes4.ts:408-442, plus ``tblorder.Enabled = 1``."""
        placeholders = ", ".join(["%s"] * len(resource.remove_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT DISTINCT
                tblstep.ONo AS orderNumber,
                tblstep.OPos AS orderPosition
            FROM
                tblstep
                LEFT JOIN tblboxpos ON tblstep.ONo = tblboxpos.ONo AND tblstep.OPos = tblboxpos.OPos
                INNER JOIN tblorderpos ON
                    tblstep.ONo = tblorderpos.ONo AND
                    tblstep.OPos = tblorderpos.OPos
                INNER JOIN tblorder ON
                    tblstep.ONo = tblorder.ONo
            WHERE
                tblstep.OpNo IN ({placeholders}) AND
                tblorderpos.`End` IS NULL AND
                tblstep.`End` IS NULL AND
                tblboxpos.BoxPos IS NULL AND
                tblorder.`End` IS NULL AND
                tblorder.Enabled = 1
            ORDER BY
                tblstep.PlannedStart,
                tblstep.ONo,
                tblstep.OPos
            LIMIT %s
            """,
            (*resource.remove_operation_numbers, limit),
        )
        return [
            DisassemblyRequest(int(order), int(position))
            for order, position in _dedupe(
                [(int(row["orderNumber"]), int(row["orderPosition"])) for row in rows]
            )
        ]

    def which_box_has_disassembly_order(
        self, resource: ResourceConfig, requested_places: int
    ) -> int:
        """mes4.ts:444-488 with ``tblboxdef.PosCount`` instead of hard-coded 8."""
        placeholders = ", ".join(["%s"] * len(resource.remove_operation_numbers))
        rows = self._db.query(
            f"""
            SELECT
                main_query.BoxId
            FROM
                (
                    SELECT DISTINCT
                        tblboxpos.BoxId,
                        tblstep.ONo,
                        tblstep.OPos
                    FROM
                        tblstep
                        LEFT JOIN tblboxpos ON tblstep.ONo = tblboxpos.ONo AND tblstep.OPos = tblboxpos.OPos
                        INNER JOIN tblorderpos ON tblstep.ONo = tblorderpos.ONo AND tblstep.OPos = tblorderpos.OPos
                    WHERE
                        tblstep.OpNo IN ({placeholders}) AND (
                            tblorderpos.End IS NOT NULL OR
                            tblstep.End IS NOT NULL OR
                            tblboxpos.BoxPos IS NOT NULL
                        )
                ) AS main_query
            LEFT JOIN tblboxpos ON main_query.ONo = tblboxpos.ONo AND main_query.OPos = tblboxpos.OPos
            LEFT JOIN tblorderpos ON main_query.ONo = tblorderpos.ONo AND main_query.OPos = tblorderpos.OPos
            LEFT JOIN tblboxdef ON tblboxdef.BoxPNo = %s
            WHERE
                tblboxpos.BoxPNo = %s AND
                tblboxpos.PNo = 0
            GROUP BY
                main_query.BoxId
            HAVING
                (COALESCE(MAX(tblboxdef.PosCount), %s) - COUNT(main_query.BoxId)) >= %s
            ORDER BY
                main_query.BoxId
            """,
            (
                *resource.remove_operation_numbers,
                self._config.box.type_pno,
                self._config.box.type_pno,
                self._config.box.capacity,
                requested_places,
            ),
        )
        return int(rows[0]["BoxId"]) if rows else 0

    # --------------------------------------------------------------- booking

    def get_part_requests_for_commissioning_orders_for_resource(
        self, resource: ResourceConfig
    ) -> list[PartRequest]:
        """mes4.ts:627-693 (requested parts + open positions per part)."""
        requested_rows = self._db.query(
            """
            SELECT
                CONVERT(part.Parameter, UNSIGNED INTEGER) AS partNumber,
                Count(tblorderpos.OPos) AS quantity
            FROM
                tblorderpos
                INNER JOIN tblstepparameter part ON
                    tblorderpos.ONo = part.ONo AND
                    tblorderpos.OPos = part.OPos AND
                    tblorderpos.StepNo = part.StepNo
                INNER JOIN tblstepparameter sourceboxid ON
                    part.StepNo = sourceboxid.StepNo AND
                    part.ONo = sourceboxid.ONo AND
                    part.OPos = sourceboxid.OPos
            WHERE
                tblorderpos.WPNo = %s AND
                tblorderpos.OpNo = 220 AND
                tblorderpos.`End` IS NULL AND
                part.ParameterNo = 11 AND /* PNo */
                sourceboxid.ParameterNo = 3 AND /* source box id */
                sourceboxid.Parameter = '0'
            GROUP BY
                part.Parameter
            """,
            (resource.workplan_number,),
        )
        requests: list[PartRequest] = []
        for requested in requested_rows:
            part_number = int(requested["partNumber"])
            orders = self._db.query(
                """
                SELECT
                    tblorderpos.ONo AS orderNumber,
                    tblorderpos.OPos AS orderPosition
                FROM
                    tblorderpos
                    INNER JOIN tblstepparameter part ON
                        tblorderpos.ONo = part.ONo AND
                        tblorderpos.OPos = part.OPos AND
                        tblorderpos.StepNo = part.StepNo
                    INNER JOIN tblstepparameter sourceboxid ON
                        part.StepNo = sourceboxid.StepNo AND
                        part.ONo = sourceboxid.ONo AND
                        part.OPos = sourceboxid.OPos
                WHERE
                    tblorderpos.WPNo = %s AND
                    tblorderpos.OpNo = 220 AND
                    tblorderpos.`End` IS NULL AND
                    part.ParameterNo = 11 AND
                    sourceboxid.ParameterNo = 3 AND
                    sourceboxid.Parameter = '0' AND
                    part.Parameter = %s
                ORDER BY
                    tblorderpos.ONo,
                    tblorderpos.OPos
                """,
                (resource.workplan_number, str(part_number)),
            )
            requests.append(
                PartRequest(
                    part_number=part_number,
                    quantity=int(requested["quantity"]),
                    orders=[
                        OrderRef(
                            order_number=int(order["orderNumber"]),
                            order_position=int(order["orderPosition"]),
                        )
                        for order in orders
                    ],
                )
            )
        return requests

    def _disabled_buffer_filter(self) -> tuple[str, tuple[Any, ...]]:
        """SQL fragment excluding boxes currently in a disabled Pick&Sort buffer."""
        disabled = self._config.pick_and_sort.disabled_buffers
        if not disabled:
            return "", ()
        placeholders = ", ".join(["%s"] * len(disabled))
        fragment = f"""
                AND NOT EXISTS (
                    SELECT 1
                    FROM tblbufferpos disabled_buffer
                    WHERE disabled_buffer.BoxID = tblboxpos.BoxId AND
                          disabled_buffer.ResourceId = %s AND
                          disabled_buffer.BufNo IN ({placeholders})
                )"""
        return fragment, (
            self._config.pick_and_sort_resource_id,
            *disabled,
        )

    def box_in_disabled_pick_sort_buffer(self, box_id: int) -> bool:
        """True when ``box_id`` currently sits in a disabled Pick&Sort buffer."""
        disabled = self._config.pick_and_sort.disabled_buffers
        if not disabled:
            return False
        placeholders = ", ".join(["%s"] * len(disabled))
        rows = self._db.query(
            f"""
            SELECT 1 AS found
            FROM tblbufferpos
            WHERE ResourceId = %s AND
                  BoxID = %s AND
                  BufNo IN ({placeholders})
            LIMIT 1
            """,
            (self._config.pick_and_sort_resource_id, box_id, *disabled),
        )
        return bool(rows)

    def fully_unbooked_asrs_boxes_with_parts(
        self, part_numbers: set[int] | list[int] | tuple[int, ...]
    ) -> dict[int, list[BoxSlot]]:
        """ASRS20 boxes whose every slot is unbooked, keyed by box id.

        Only slots holding one of ``part_numbers`` are returned.  "Every slot
        unbooked" means ``ONo = 0 AND OPos = 0`` for all slots, so no other
        order's source part can leave with the box.  A box sitting in a
        disabled Pick&Sort buffer is excluded like any other source box.

        In read-only dry-run mode a box that was touched by an earlier shadow
        booking in the same cycle is dropped: its real rows still look
        unbooked, but the live planner would no longer see it as free.
        """
        requested = {int(part_number) for part_number in part_numbers}
        if not requested:
            return {}
        disabled_sql, disabled_params = self._disabled_buffer_filter()
        rows = self._db.query(
            f"""
            SELECT DISTINCT
                tblboxpos.BoxId AS boxId,
                tblboxpos.BoxPos AS boxPosition,
                tblboxpos.PNo AS partNumber
            FROM
                tblbufferpos
                INNER JOIN tblboxpos ON
                    tblbufferpos.PNo = tblboxpos.BoxPNo AND
                    tblbufferpos.BoxID = tblboxpos.BoxId
            WHERE
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BoxID > 0 AND
                tblboxpos.BoxPNo = %s AND
                NOT EXISTS (
                    SELECT 1
                    FROM tblboxpos booked
                    WHERE
                        booked.BoxId = tblboxpos.BoxId AND
                        booked.BoxPNo = tblboxpos.BoxPNo AND
                        (booked.ONo <> 0 OR booked.OPos <> 0)
                )
                {disabled_sql}
            ORDER BY
                tblboxpos.BoxId,
                tblboxpos.BoxPos
            """,
            (self._config.asrs_resource_id, self._config.box.type_pno, *disabled_params),
        )

        shadow_boxes: set[int] = set()
        if self._db.dry_run:
            shadow_boxes = {box_id for box_id, _ in self._shadow_source_slots}
            shadow_boxes.update(self._shadow_target_slots)

        boxes: dict[int, list[BoxSlot]] = {}
        for row in rows:
            box_id = int(row["boxId"])
            if box_id in shadow_boxes:
                continue
            part_number = _int_or_zero(row.get("partNumber"))
            if part_number not in requested:
                continue
            boxes.setdefault(box_id, []).append(
                BoxSlot(
                    box_id=box_id,
                    box_position=int(row["boxPosition"]),
                    part_number=part_number,
                )
            )
        return boxes

    def get_box_positions_for_parts_for_resource(
        self, resource: ResourceConfig, part_number: int, quantity: int
    ) -> list[BoxPosition]:
        """mes4.ts:778-878 (used boxes first, then fullest matching boxes)."""
        disabled_sql, disabled_params = self._disabled_buffer_filter()
        # Dry-run: over-fetch by the slots already "booked" this cycle and
        # drop them, so a slot is never handed out twice in one shadow cycle.
        shadow = self._shadow_source_slots if self._db.dry_run else set()
        used_rows = self._db.query(
            f"""
            SELECT DISTINCT
                CONVERT(sourceboxid.Parameter, UNSIGNED INTEGER) AS boxId,
                tblboxpos.BoxPos AS boxPosition
            FROM
                tblstepparameter part
                INNER JOIN tblstepparameter sourceboxid ON
                    part.StepNo = sourceboxid.StepNo AND
                    part.ONo = sourceboxid.ONo AND
                    part.OPos = sourceboxid.OPos
                INNER JOIN tblboxpos ON
                    sourceboxid.Parameter = tblboxpos.BoxId
            WHERE
                part.ParameterNo = 11 AND
                part.Parameter = %s AND
                sourceboxid.ParameterNo = 3 AND
                sourceboxid.Parameter > 0 AND
                part.WPNo = %s AND
                tblboxpos.PNo = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0
                {disabled_sql}
            ORDER BY
                BoxId,
                tblboxpos.BoxPos
            LIMIT %s
            """,
            (
                str(part_number),
                resource.workplan_number,
                part_number,
                *disabled_params,
                quantity + len(shadow),
            ),
        )
        used = [
            BoxPosition(box_id=int(row["boxId"]), box_position=int(row["boxPosition"]))
            for row in used_rows
            if (int(row["boxId"]), int(row["boxPosition"])) not in shadow
        ][:quantity]

        remaining = max(0, quantity - len(used))
        if remaining == 0:
            return used

        empty_rows = self._db.query(
            f"""
            SELECT DISTINCT
                tblboxpos.BoxId AS boxId,
                tblboxpos.BoxPos AS boxPosition
            FROM
                tblboxpos
                INNER JOIN tblbufferpos ON tblbufferpos.BoxID = tblboxpos.BoxId
                INNER JOIN (SELECT
                    tblboxpos.BoxId,
                    Count(tblboxpos.BoxPos) AS Count_BoxPos
                FROM
                    tblboxpos
                WHERE
                    tblboxpos.PNo = %s AND
                    tblboxpos.OPos = 0 AND
                    tblboxpos.ONo = 0
                GROUP BY
                    tblboxpos.BoxId
                ORDER BY
                    Count_BoxPos DESC) MostFullBox ON tblboxpos.BoxId = MostFullBox.BoxId
            WHERE
                tblboxpos.PNo = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblbufferpos.PNo = %s AND
                tblbufferpos.ResourceId = %s AND
                tblboxpos.BoxId NOT IN (SELECT
                    sourceboxid.Parameter AS BoxId
                FROM
                    tblstepparameter part
                    INNER JOIN tblstepparameter sourceboxid ON
                        part.StepNo = sourceboxid.StepNo AND
                        part.ONo = sourceboxid.ONo AND
                        part.OPos = sourceboxid.OPos
                    INNER JOIN tblboxpos ON sourceboxid.Parameter = tblboxpos.BoxId
                WHERE
                    part.Parameter = tblboxpos.PNo AND
                    part.WPNo = %s AND
                    tblboxpos.PNo = %s AND
                    part.ParameterNo = 11 AND
                    sourceboxid.ParameterNo = 3 AND
                    sourceboxid.Parameter > 0 AND
                    tblboxpos.ONo = 0 AND
                    tblboxpos.OPos = 0
                ORDER BY
                    BoxId,
                    tblboxpos.BoxPos)
                {disabled_sql}
            ORDER BY
                MostFullBox.Count_BoxPos DESC,
                tblboxpos.BoxId,
                tblboxpos.BoxPos
            LIMIT %s
            """,
            (
                part_number,
                part_number,
                self._config.box.type_pno,
                self._config.asrs_resource_id,
                resource.workplan_number,
                part_number,
                *disabled_params,
                remaining + len(shadow),
            ),
        )
        used.extend(
            [
                BoxPosition(box_id=int(row["boxId"]), box_position=int(row["boxPosition"]))
                for row in empty_rows
                if (int(row["boxId"]), int(row["boxPosition"])) not in shadow
            ][:remaining]
        )
        # The buffer join can list the same box in more than one buffer slot.
        # Booking the same (box, position) twice would fail the guarded UPDATE
        # after the first assignment, so keep the first occurrence.
        seen: set[tuple[int, int]] = set()
        unique: list[BoxPosition] = []
        for position in used:
            key = (position.box_id, position.box_position)
            if key in seen:
                continue
            seen.add(key)
            unique.append(position)
        return unique

    def source_slot_candidates(
        self, resource: ResourceConfig, part_number: int
    ) -> list[SourceSlotCandidate]:
        """Every slot :meth:`get_box_positions_for_parts_for_resource` may return.

        Same filters as that query, without ``ORDER BY ... LIMIT``: a free slot
        holding the part, not in a disabled Pick & Sort buffer, in a box that
        is either already a source for the part in the resource's workplan
        or stored in ASRS20.  The facts behind the ordering are returned per
        slot so a policy (``decisions.RulePolicy``) can choose.
        """
        disabled_sql, disabled_params = self._disabled_buffer_filter()
        shadow = self._shadow_source_slots if self._db.dry_run else set()
        rows = self._db.query(
            f"""
            SELECT
                tblboxpos.BoxId AS boxId,
                tblboxpos.BoxPos AS boxPosition,
                EXISTS (
                    SELECT 1
                    FROM
                        tblstepparameter part
                        INNER JOIN tblstepparameter sourceboxid ON
                            part.StepNo = sourceboxid.StepNo AND
                            part.ONo = sourceboxid.ONo AND
                            part.OPos = sourceboxid.OPos
                    WHERE
                        part.ParameterNo = 11 AND
                        part.Parameter = %s AND
                        part.WPNo = %s AND
                        sourceboxid.ParameterNo = 3 AND
                        sourceboxid.Parameter > 0 AND
                        sourceboxid.Parameter = tblboxpos.BoxId
                ) AS usedAsSource,
                (
                    SELECT MIN(asrs.BufPos)
                    FROM tblbufferpos asrs
                    WHERE asrs.BoxID = tblboxpos.BoxId AND
                          asrs.ResourceId = %s AND
                          asrs.PNo = %s
                ) AS asrsBufPos,
                (
                    SELECT COUNT(*) FROM tblboxpos c
                    WHERE c.BoxId = tblboxpos.BoxId AND c.PNo = tblboxpos.PNo AND
                          c.ONo = 0 AND c.OPos = 0
                ) AS freeOfPart,
                (
                    SELECT COUNT(*) FROM tblboxpos c
                    WHERE c.BoxId = tblboxpos.BoxId AND c.PNo = 0 AND
                          c.ONo = 0 AND c.OPos = 0
                ) AS emptySlots,
                (
                    SELECT COUNT(*) FROM tblboxpos c
                    WHERE c.BoxId = tblboxpos.BoxId AND c.PNo > 0
                ) AS partSlots
            FROM
                tblboxpos
            WHERE
                tblboxpos.PNo = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0
                {disabled_sql}
            ORDER BY
                tblboxpos.BoxId,
                tblboxpos.BoxPos
            """,
            (
                str(part_number),
                resource.workplan_number,
                self._config.asrs_resource_id,
                self._config.box.type_pno,
                part_number,
                *disabled_params,
            ),
        )
        candidates = []
        for row in rows:
            key = (int(row["boxId"]), int(row["boxPosition"]))
            used = bool(row["usedAsSource"])
            asrs = _int_or_none(row.get("asrsBufPos"))
            if key in shadow or not (used or asrs is not None):
                continue
            candidates.append(
                SourceSlotCandidate(
                    box_id=key[0],
                    box_position=key[1],
                    part_number=part_number,
                    used_as_source=used,
                    asrs_buf_pos=asrs,
                    free_of_part_in_box=int(row["freeOfPart"]),
                    empty_slots_in_box=int(row["emptySlots"]),
                    part_slots_in_box=int(row["partSlots"]),
                )
            )
        return candidates

    def book_order_to_box_position(
        self,
        order_number: int,
        order_position: int,
        box_id: int,
        box_position: int,
        part_number: int,
    ) -> None:
        """mes4.ts:880-900, guarded by the expected part and a free slot."""
        if self._db.dry_run:
            self._shadow_source_slots.add((box_id, box_position))
        self._rowcount_one(
            """
            UPDATE
                tblboxpos
            SET
                tblboxpos.ONo = %s,
                tblboxpos.OPos = %s
            WHERE
                tblboxpos.BoxId = %s AND
                tblboxpos.BoxPos = %s AND
                tblboxpos.ONo = 0 AND
                tblboxpos.OPos = 0 AND
                tblboxpos.PNo = %s
            """,
            (order_number, order_position, box_id, box_position, part_number),
            f"booking source box {box_id}.{box_position} (PNo {part_number}) "
            f"to order {order_number}.{order_position}",
            concurrent=True,
        )

    def book_box_as_source_box_id_to_order(
        self, order_number: int, order_position: int, box_id: int
    ) -> None:
        """mes4.ts:902-922 (ParameterNo 3)."""
        self._rowcount_one(
            """
            UPDATE
                tblstepparameter
            SET
                tblstepparameter.Parameter = %s
            WHERE
                tblstepparameter.ONo = %s AND
                tblstepparameter.OPos = %s AND
                tblstepparameter.StepNo = 10 AND
                tblstepparameter.ParameterNo = 3
            """,
            (str(box_id), order_number, order_position),
            f"writing source box id {box_id} to order {order_number}.{order_position}",
        )

    def book_box_pos_as_source_box_pos_to_order(
        self, order_number: int, order_position: int, box_position: int
    ) -> None:
        """mes4.ts:924-944 (ParameterNo 4)."""
        self._rowcount_one(
            """
            UPDATE
                tblstepparameter
            SET
                tblstepparameter.Parameter = %s
            WHERE
                tblstepparameter.ONo = %s AND
                tblstepparameter.OPos = %s AND
                tblstepparameter.StepNo = 10 AND
                tblstepparameter.ParameterNo = 4
            """,
            (str(box_position), order_number, order_position),
            f"writing source box position {box_position} to order {order_number}.{order_position}",
        )

    def is_box_already_on_the_way_to_resource(
        self, resource: ResourceConfig, box_id: int
    ) -> bool:
        """mes4.ts:969-991."""
        rows = self._db.query(
            """
            SELECT
                Count(tblboxpos.BoxPos) = 0 AS result
            FROM
                tblboxpos
                INNER JOIN tblorderpos ON
                    tblboxpos.ONo = tblorderpos.ONo AND
                    tblboxpos.OPos = tblorderpos.OPos
            WHERE
                tblboxpos.BoxId = %s AND
                tblorderpos.State = 0 AND
                tblorderpos.WPNo = %s
            """,
            (box_id, resource.workplan_number),
        )
        return bool(rows) and bool(rows[0].get("result"))

    # --------------------------------------------------------- buffer locking

    def pick_sort_positions_to_lock(self) -> list[int]:
        """mes4.ts:993-1025."""
        rows = self._db.query(
            """
            SELECT
                tblbufferpos.BufNo AS bufferNumber
            FROM
                tblbufferpos
                INNER JOIN tblboxpos ON tblboxpos.BoxPNo = tblbufferpos.PNo AND tblbufferpos.BoxID = tblboxpos.BoxId
                LEFT JOIN tblorderpos ON tblboxpos.ONo = tblorderpos.ONo AND tblboxpos.OPos = tblorderpos.OPos
            WHERE
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BufPos = 1 AND
                tblbufferpos.PNo > 0 AND
                tblbufferpos.BoxID > 0 AND
                tblorderpos.ResourceID = %s
            GROUP BY
                tblbufferpos.BufNo
            UNION
            SELECT
                tblbufferpos.BufNo
            FROM
                tblbufferpos
            WHERE
                tblbufferpos.PNo = 0 AND
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BufPos = 1
            """,
            (
                self._config.pick_and_sort_resource_id,
                self._config.pick_and_sort_resource_id,
                self._config.pick_and_sort_resource_id,
            ),
        )
        return [int(row["bufferNumber"]) for row in rows]

    def pick_sort_position_is_free(self, position: int) -> bool:
        """mes4.ts:1027-1045."""
        rows = self._db.query(
            """
            SELECT
                tblbufferpos.BufNo as bufferNumber
            FROM
                tblbufferpos
            WHERE
                tblbufferpos.BufNo = %s AND
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.PNo = 0 AND
                tblbufferpos.BoxID = 0
            GROUP BY
                tblbufferpos.BufNo
            HAVING
                Count(tblbufferpos.BufPos) = 2
            """,
            (position, self._config.pick_and_sort_resource_id),
        )
        return bool(rows)

    def pick_sort_lock_states(self) -> dict[int, bool]:
        """Actual ``Booked`` state keyed by BufNo (mes4.ts:1047-1079).

        The TypeScript code converted the ordered query result to an array and
        indexed it by ``position - 1``.  Real BufNo values need not be
        contiguous, so this method returns a mapping instead.
        """
        rows = self._db.query(
            """
            SELECT
                tblbufferpos.BufNo AS bufferNumber,
                tblbufferpos.Booked AS isBooked
            FROM
                tblbufferpos
            WHERE
                tblbufferpos.ResourceId = %s AND
                tblbufferpos.BufPos = 1
            ORDER BY
                tblbufferpos.BufNo
            """,
            (self._config.pick_and_sort_resource_id,),
        )
        return {int(row["bufferNumber"]): bool(row["isBooked"]) for row in rows}

    # ------------------------------------------------------------ dry-run MES

    def max_order_number(self) -> int:
        """Highest existing ONo, used only to number read-only log entries."""
        row = self._db.query_one("SELECT MAX(ONo) AS maxOno FROM tblorder")
        return _int_or_zero(row.get("maxOno") if row else 0)

    def max_order_position(self, order_number: int) -> int:
        """Highest existing OPos for ``order_number`` (read-only logging)."""
        row = self._db.query_one(
            "SELECT MAX(OPos) AS maxOpos FROM tblorderpos WHERE ONo = %s",
            (order_number,),
        )
        return _int_or_zero(row.get("maxOpos") if row else 0)

    def simulate_mes_commissioning_order(
        self,
        *,
        workplan_number: int,
        box_id: int,
        main_order_number: int,
        main_order_position: int,
        main_step_number: int,
        part_number: int,
        order_number: int,
    ) -> tuple[int, int]:
        """Fake the DB rows MES4 creates for 101/6 (dry-run only).

        The rows are inserted through the same connection/transaction as the
        planner writes, so later reads in the cycle see them.  A new order gets
        ``max(ONo) + 1``; an appended position gets ``max(OPos) + 1``.
        """
        if not self._db.simulate:
            raise DatabaseError("simulate_mes_commissioning_order is a simulate-only operation")
        order_number, order_position = self._next_order_slot(order_number)
        self._ensure_order_row(order_number)
        self._db.execute(
            """
            INSERT INTO tblorderpos
                (ONo, OPos, WPNo, StepNo, OpNo, State)
            VALUES
                (%s, %s, %s, 10, 220, 0)
            """,
            (order_number, order_position, workplan_number),
        )
        parameters = {
            3: "0",  # source box id
            4: "0",  # source box position
            6: str(box_id),  # target box id
            7: "0",  # target box position (written later by the planner)
            8: str(main_order_number),
            9: str(main_order_position),
            10: str(main_step_number),
            11: str(part_number),
        }
        for parameter_no, value in parameters.items():
            self._db.execute(
                """
                INSERT INTO tblstepparameter
                    (WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType)
                VALUES
                    (%s, 10, %s, %s, %s, %s, 2)
                """,
                (workplan_number, order_number, order_position, parameter_no, value),
            )
        log.info(
            "[simulate] simulated MES 101/6: ONo=%s OPos=%s WPNo=%s box=%s part=%s",
            order_number,
            order_position,
            workplan_number,
            box_id,
            part_number,
        )
        return order_number, order_position

    def simulate_mes_disassembly_order(
        self,
        *,
        workplan_disassembly_number: int,
        main_order_number: int,
        main_order_position: int,
        order_number: int,
    ) -> tuple[int, int]:
        """Fake the DB rows MES4 creates for a disassembly 101/6 (dry-run only)."""
        if not self._db.simulate:
            raise DatabaseError("simulate_mes_disassembly_order is a simulate-only operation")
        order_number, order_position = self._next_order_slot(order_number)
        self._ensure_order_row(order_number)
        self._db.execute(
            """
            INSERT INTO tblorderpos
                (ONo, OPos, WPNo, StepNo, OpNo, State)
            VALUES
                (%s, %s, %s, 10, 220, 0)
            """,
            (order_number, order_position, workplan_disassembly_number),
        )
        log.info(
            "[simulate] simulated MES 101/6: ONo=%s OPos=%s WPNo=%s main=%s.%s",
            order_number,
            order_position,
            workplan_disassembly_number,
            main_order_number,
            main_order_position,
        )
        return order_number, order_position

    # ---------------------------------------------------------------- helpers

    def _next_order_slot(self, requested_order_number: int) -> tuple[int, int]:
        if requested_order_number == 0:
            row = self._db.query_one("SELECT MAX(ONo) AS maxOno FROM tblorder")
            order_number = _int_or_zero(row.get("maxOno") if row else 0) + 1
            return order_number, 1
        row = self._db.query_one(
            "SELECT MAX(OPos) AS maxOpos FROM tblorderpos WHERE ONo = %s",
            (requested_order_number,),
        )
        order_position = _int_or_zero(row.get("maxOpos") if row else 0) + 1
        return requested_order_number, order_position

    def _ensure_order_row(self, order_number: int) -> None:
        row = self._db.query_one("SELECT ONo FROM tblorder WHERE ONo = %s", (order_number,))
        if row is None:
            self._db.execute(
                "INSERT INTO tblorder (ONo, State, Enabled) VALUES (%s, 0, 1)",
                (order_number,),
            )

    def _rowcount_one(
        self,
        sql: str,
        params: tuple[Any, ...],
        description: str,
        *,
        concurrent: bool = False,
    ) -> None:
        rowcount = self._db.execute(sql, params)
        if rowcount != 1:
            error = ConcurrentWriteError if concurrent else QueryRowcountError
            raise error(f"{description}: expected 1 row, updated {rowcount}")
