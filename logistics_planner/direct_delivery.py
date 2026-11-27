"""Direct delivery: send a fully unbooked ASRS20 box straight to a station.

Pick & Sort normally moves every part from a source box into an empty target
box (WP 1–5, step 10, op 220) and the target box is then stored at the station
(step 20, op 215).  If one ASRS20 box already contains every part that one
main order position needs, that move is wasted work and a Robotino round trip.

Direct delivery uses the existing store workplans 6–9.  Their single step 10 /
op 215 stores the box to the station; its op-end SQL re-books every
``tblboxpos`` slot booked to the sub-order ``(ONo, OPos)`` onto the main
position from parameters 3/4.  That is the same re-booking WP 1–3 step 20
performs, so the main feed step (op 323/324/325/350) then finds its part via
``tblboxpos.ONo/OPos = main`` and ``PNo = part`` at the station.

The decision itself is a pure function (:func:`select_direct_delivery`) so the
coverage, FIFO and tie-break rules can be tested directly.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence

from .config import Config, ResourceConfig
from .errors import PlannerError
from .mes_tcp import MesTcp
from .queries import BoxSlot, DirectDemandGroup, Repository

log = logging.getLogger(__name__)

Selection = tuple[int, list[tuple[DirectDemandGroup, list[BoxSlot]]]]


def _multiset(group: DirectDemandGroup) -> Counter[int]:
    return Counter(part.part_number for part in group.parts)


def _slot_multiset(slots: Sequence[BoxSlot]) -> Counter[int]:
    return Counter(slot.part_number for slot in slots)


def select_direct_delivery(
    groups: Sequence[DirectDemandGroup],
    boxes: Mapping[int, Sequence[BoxSlot]],
    capacity: int,
) -> Selection | None:
    """Choose one box and the FIFO-ordered groups it serves.

    A group fits a box when the box's remaining part multiset covers every
    part in the group (duplicates count, so two 800s need two 800 slots).
    Candidate boxes are those that fit the first fitting group in FIFO order.
    Each candidate then takes groups greedily in FIFO order while they fit.
    The winner serves the most groups, then the most parts, then the lowest
    box id.

    Returns ``(box_id, [(group, [BoxSlot, ...]), ...])`` or ``None``.
    """
    if not groups or capacity <= 0:
        return None

    boxes_by_id = {
        int(box_id): sorted(slots, key=lambda slot: (slot.box_id, slot.box_position))
        for box_id, slots in boxes.items()
        if slots
    }
    if not boxes_by_id:
        return None

    candidates: list[int] = []
    for group in groups:
        needed = _multiset(group)
        if sum(needed.values()) > capacity:
            continue
        candidates = [
            box_id
            for box_id, slots in boxes_by_id.items()
            if needed <= _slot_multiset(slots)
        ]
        if candidates:
            break
    if not candidates:
        return None

    best: Selection | None = None
    best_key: tuple[int, int, int] | None = None
    for box_id in sorted(candidates):
        remaining = _slot_multiset(boxes_by_id[box_id])
        pools: dict[int, list[BoxSlot]] = {}
        for slot in boxes_by_id[box_id]:
            pools.setdefault(slot.part_number, []).append(slot)

        assignments: list[tuple[DirectDemandGroup, list[BoxSlot]]] = []
        assigned_parts = 0
        for group in groups:
            needed = _multiset(group)
            if assigned_parts + sum(needed.values()) > capacity:
                continue
            if not needed <= remaining:
                continue
            chosen = [
                pools[part.part_number].pop(0) for part in group.parts
            ]
            remaining.subtract(needed)
            assigned_parts += sum(needed.values())
            assignments.append((group, chosen))

        key = (len(assignments), assigned_parts, -box_id)
        if best_key is None or key > best_key:
            best_key = key
            best = (box_id, assignments)
    return best


def plan_direct_delivery(
    repository: Repository,
    mes: MesTcp,
    config: Config,
    resource: ResourceConfig,
) -> bool:
    """Plan one direct-delivery bundle for ``resource``.

    Returns ``True`` when a store box was planned.  The 101/6 calls are made
    outside any DB transaction (same rule as commissioning and disassembly);
    each group's source slots are then booked in one transaction.  All groups
    share one sub-order ``ONo`` (``#ONo=<returned ONo>``), as disassembly does.

    If MES accepts a store order but the local booking fails, the sub-order is
    left open and blocks the resource until an operator fixes it by hand.  The
    error log names its ``ONo/OPos``; the exception is re-raised so the cycle
    aborts.
    """
    groups = repository.direct_delivery_demands(resource)
    if not groups:
        return False

    part_numbers = sorted(
        {part.part_number for group in groups for part in group.parts}
    )
    boxes = repository.fully_unbooked_asrs_boxes_with_parts(part_numbers)
    selection = select_direct_delivery(groups, boxes, config.box.capacity)
    if selection is None:
        return False

    box_id, assignments = selection
    prefix = "[dry-run] " if repository.read_only else ""
    order_number = 0

    for group, slots in assignments:
        # Never hold a DB transaction/lock across the MES4 TCP call.
        order = mes.create_disassembly_order(
            workplan_disassembly_number=resource.workplan_disassembly_number,
            main_order_number=group.order_number,
            main_order_position=group.order_position,
            order_number=order_number,
        )
        try:
            with repository.transaction():
                for slot in slots:
                    repository.book_order_to_box_position(
                        order_number=order.order_number,
                        order_position=order.order_position,
                        box_id=slot.box_id,
                        box_position=slot.box_position,
                        part_number=slot.part_number,
                    )
        except Exception as exc:
            log.error(
                "Direct delivery: MES accepted store sub-order %s.%s for %s "
                "(main %s.%s, box %s) but the local slot booking failed: %s; "
                "the open store order blocks %s until it is fixed by hand",
                order.order_number,
                order.order_position,
                resource.name,
                group.order_number,
                group.order_position,
                box_id,
                exc,
                resource.name,
            )
            raise

        # Use the same ONo for the next group's store position.
        order_number = order.order_number
        log.info(
            "%sdirect delivery: resource=%s main=%s.%s sub=%s.%s box=%s "
            "slots=%s parts=%s",
            prefix,
            resource.name,
            group.order_number,
            group.order_position,
            order.order_number,
            order.order_position,
            box_id,
            [slot.box_position for slot in slots],
            [part.part_number for part in group.parts],
        )

    return True
