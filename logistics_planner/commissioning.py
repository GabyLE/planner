"""Planning and booking of commissioning (Pick & Sort) orders.

Ported from ``create-commissioning-orders.ts`` and
``book-commissioning-orders.ts``.  Policy changes relative to the TypeScript
implementation are marked with ``FIX``:

* the number of positions planned into a box is capped by the box's actual
  free-slot count (the reported over-planning bug);
* the chosen free slot is locked (``SELECT ... FOR UPDATE``) and updated with a
  guarded UPDATE before MES4 is asked to append the position, and ParameterNo 7
  is only written after the returned ONo/OPos are known;
* a missing source part skips that part instead of aborting the whole
  resource's booking loop.
"""

from __future__ import annotations

import logging

from .config import Config, ResourceConfig
from .direct_delivery import plan_direct_delivery
from .errors import CapacityError, PlannerError, PlanningError
from .mes_tcp import MesTcp
from .decisions import (
    DecisionLog,
    NullDecisionLog,
    RulePolicy,
    SourcePolicy,
    SourceSlotContext,
    after_source_pick,
)
from .queries import BoxPosition, OrderRef, PartDemand, PartRequest, Repository

log = logging.getLogger(__name__)


def create_commissioning_orders(repository: Repository, mes: MesTcp, config: Config) -> None:
    """mes4.ts:19-156 / create-commissioning-orders.ts:19-156."""
    log.info("/////////////////////////////////")
    log.info("/ Planning commissioning orders /")
    log.info("/////////////////////////////////")
    log.info("")

    resources = config.resources
    start_index = 0

    # Start with the first resource that already has a commissioning order.
    for index, resource in enumerate(resources):
        if repository.there_is_a_commissioning_order_for_resource(resource):
            log.info("The %s has already a commissioning order", resource.name)
            log.info("skip other resources")
            log.info("")
            start_index = index
            break

    for index in range(start_index, len(resources)):
        resource = resources[index]
        log.info("----------")
        log.info("| %s |", resource.name)
        log.info("----------")
        log.info("")

        parts = repository.which_parts_to_commission_for_resource(resource, config.box.capacity)
        has_order = repository.there_is_a_commissioning_order_for_resource(resource)

        if not parts:
            if has_order:
                log.info("Commissioning order already created for %s", resource.name)
                log.info("and there is no more commissioning to do")
                break  # Do not continue with other resources
            log.info("There is no commissioning to do")
            continue

        # The station buffer must be free for the commissioned box.
        if not repository.resource_buffer_has_empty_place(resource):
            log.info("There is already a box at %s", resource.name)
            log.info("We do not need to plan commissioning orders")
            continue
        log.info("There is no box at %s", resource.name)

        if (
            resource.direct_delivery != "off"
            and repository.there_is_an_open_store_order_for_resource(resource)
        ):
            log.info(
                "The direct/store box is already on the way to %s; waiting",
                resource.name,
            )
            continue

        box_id = 0
        order_number = 0
        active = None
        if has_order:
            try:
                active = repository.active_commissioning_order(resource)
            except PlanningError as exc:
                # An inconsistent DB state must not be guessed away.  Log it
                # and skip this resource for the cycle (docs policy).
                log.error("Cannot plan %s this cycle: %s", resource.name, exc)
                continue
            if active is None:
                log.warning(
                    "%s has an enabled commissioning order but no open position; "
                    "planning a new order",
                    resource.name,
                )

        if active is not None:
            box_id = active.box_id
            order_number = active.order_number
            log.info(
                "There is already a commissioning order %s in box %s", order_number, box_id
            )

            if repository.box_in_disabled_pick_sort_buffer(box_id):
                log.error(
                    "Commissioning box %s for %s is in a disabled Pick&Sort buffer; "
                    "not planning any commissioning until it is moved",
                    box_id,
                    resource.name,
                )
                # Stop, like "box already on the way": planning the next
                # resource would start a second commissioning order and break
                # the one-commissioning-order-at-a-time rule.
                break

            if repository.is_box_already_on_the_way_to_resource(resource, box_id):
                log.info("The box is already on the way to %s", resource.name)
                break  # Do not continue with other resources
            log.info("The robot not yet completely commissioned the box, it is still at Pick&Sort")

            if not repository.box_has_empty_place(box_id):
                log.info(", but there is no more empty place in the box")
                log.info(", so it is not possible to plan more commissioning order at the moment")
                break  # Do not continue with other resources
            log.info("and there is still empty place available in the box")
        else:
            if repository.there_is_a_commissioned_box_at_resource(resource):
                log.info("The commissioned box is already at resource")
                continue  # Continue with the next resource

            if resource.direct_delivery != "off":
                if plan_direct_delivery(repository, mes, config, resource):
                    break  # One resource is planned per cycle, like Pick & Sort
                if resource.direct_delivery == "only":
                    log.info(
                        "No ASRS20 box covers the demand; waiting at %s",
                        resource.name,
                    )
                    continue

            log.info("There is no active commissioning order yet")
            box_id = repository.which_box_to_use_for_new_commissioning_orders(config.box.capacity)
            if box_id == 0:
                log.info("No empty box available in ASRS20")
                continue  # Continue with other resources
            log.info("Box %s is selected as new box for commissioning orders", box_id)

        # FIX: never plan more positions than the target box has free slots.
        free_slots = repository.count_free_box_slots(box_id)
        if free_slots <= 0:
            log.info("Box %s has no free place left; not planning more orders", box_id)
            break
        selected_parts = parts[:free_slots]
        if len(selected_parts) < len(parts):
            log.info(
                "Box %s has only %s free place(s); planning %s of %s part request(s)",
                box_id,
                free_slots,
                len(selected_parts),
                len(parts),
            )

        log.info("There are parts to be commissioned for %s: %s", resource.name, selected_parts)

        for part in selected_parts:
            created = _create_and_book_one_part(
                repository=repository,
                mes=mes,
                resource=resource,
                box_id=box_id,
                part=part,
                order_number=order_number,
            )
            # Use the same ONo for the next commissioning order of this resource.
            order_number = created.order_number
            log.info(
                "Commissioning order created: ONo=%s OPos=%s part=%s",
                created.order_number,
                created.order_position,
                part.part_number,
            )

        break  # Only one resource is planned per cycle, like the TS planner

    log.info("")


def reconcile_unbooked_positions(
    repository: Repository,
    config: Config,
    reported: set[tuple[int, int]] | None = None,
) -> None:
    """Repair open commissioning positions whose target slot is missing.

    Runs at the start of every plan cycle.  It covers both the old TypeScript
    ``ParameterNo 7 = 0`` orphans and positions left behind when MES4 accepted
    101/6 but the local booking failed.

    A position that cannot be repaired automatically (full or unknown target
    box, box in a disabled buffer, failed write) is reported as an ERROR
    **once** and left for manual repair.  ``reported`` holds the ``(ONo,
    OPos)`` keys already reported; the caller keeps it across cycles.  Later
    cycles retry silently (DEBUG).  A key is forgotten once the position no
    longer shows up, so a position that becomes an orphan again is reported
    again.  Without ``reported`` every cycle reports every orphan.
    """
    positions = repository.unbooked_commissioning_positions()
    if reported is None:
        reported = set()
    current = {(position.order_number, position.order_position) for position in positions}
    reported &= current
    if not positions:
        return
    new_positions = current - reported
    if new_positions:
        log.warning(
            "Found %s unbooked commissioning position(s) (%s new); reconciling",
            len(positions),
            len(new_positions),
        )
    else:
        log.debug("Retrying %s known unbooked commissioning position(s)", len(positions))

    for position in positions:
        key = (position.order_number, position.order_position)
        label = f"{position.order_number}.{position.order_position}"

        def report(message: str, *args) -> None:
            if key in reported:
                log.debug(message + " (already reported)", *args)
            else:
                log.error(message + "; fix it by hand (reported once)", *args)
                reported.add(key)

        if position.box_id <= 0:
            report("Cannot reconcile commissioning position %s: target box is unknown", label)
            continue
        if repository.box_in_disabled_pick_sort_buffer(position.box_id):
            report(
                "Cannot reconcile commissioning position %s: target box %s is in a "
                "disabled Pick&Sort buffer",
                label,
                position.box_id,
            )
            continue
        try:
            with repository.transaction():
                if position.existing_box_position is not None:
                    box_position = position.existing_box_position
                else:
                    box_position = repository.reserve_free_box_slot(position.box_id)
                    if box_position <= 0:
                        raise CapacityError(
                            f"box {position.box_id} returned invalid position {box_position}"
                        )
                    repository.book_reserved_box_slot(
                        box_id=position.box_id,
                        box_position=box_position,
                        order_number=position.order_number,
                        order_position=position.order_position,
                    )
                repository.book_box_pos_as_target_box_pos_to_order(
                    order_number=position.order_number,
                    order_position=position.order_position,
                    box_position=box_position,
                )
        except CapacityError as exc:
            report(
                "Cannot reconcile commissioning position %s: target box %s is full (%s)",
                label,
                position.box_id,
                exc,
            )
        except PlannerError as exc:
            report("Cannot reconcile commissioning position %s: %s", label, exc)
        else:
            reported.discard(key)
            if repository.read_only:
                log.info(
                    "[dry-run] would book commissioning position %s to box %s, position %s",
                    label,
                    position.box_id,
                    box_position,
                )
            else:
                log.info(
                    "Reconciled commissioning position %s to box %s, position %s",
                    label,
                    position.box_id,
                    box_position,
                )


def reconcile_stale_source_bookings(
    repository: Repository,
    reported: set[tuple[int, int]] | None = None,
) -> None:
    """Release source bookings whose slot no longer holds the booked part.

    Runs at the start of every plan cycle, before source booking.  For each
    not-yet-started step-10 position whose parameters 3/4 point at a slot that
    lost its reservation or holds another part, the reservation (if it is
    still ours) is cleared and parameters 3/4 are reset to 0 in one
    transaction.  The source-booking stage of the same cycle then books a
    valid slot.  A failed repair is reported as an ERROR once per position
    (``reported`` as in :func:`reconcile_unbooked_positions`).
    """
    stale = repository.stale_source_bookings()
    if reported is None:
        reported = set()
    current = {(item.order_number, item.order_position) for item in stale}
    reported &= current

    for item in stale:
        key = (item.order_number, item.order_position)
        label = f"{item.order_number}.{item.order_position}"
        if item.slot_part_number is None:
            found = "no such slot"
        else:
            found = (
                f"slot holds PNo {item.slot_part_number} reserved for "
                f"{item.slot_order_number}.{item.slot_order_position}"
            )
        prefix = "[dry-run] would rebook" if repository.read_only else "Rebooking"
        (log.debug if key in reported else log.warning)(
            "%s source of commissioning position %s: booked slot %s.%s for PNo %s "
            "is no longer valid (%s)",
            prefix,
            label,
            item.box_id,
            item.box_position,
            item.part_number,
            found,
        )
        try:
            with repository.transaction():
                if item.slot_still_reserved:
                    repository.release_source_slot(
                        box_id=item.box_id,
                        box_position=item.box_position,
                        order_number=item.order_number,
                        order_position=item.order_position,
                    )
                repository.reset_source_booking(
                    order_number=item.order_number,
                    order_position=item.order_position,
                    box_id=item.box_id,
                    box_position=item.box_position,
                )
        except PlannerError as exc:
            if key in reported:
                log.debug("Cannot release stale source of %s: %s (already reported)", label, exc)
            else:
                log.error(
                    "Cannot release stale source of commissioning position %s: %s; "
                    "fix it by hand (reported once)",
                    label,
                    exc,
                )
                reported.add(key)
        else:
            reported.discard(key)


def _create_and_book_one_part(
    *,
    repository: Repository,
    mes: MesTcp,
    resource: ResourceConfig,
    box_id: int,
    part: PartDemand,
    order_number: int,
):
    """One capacity-safe part booking (fix for the reported over-planning bug).

    1. send MES 101/6 *outside* any DB transaction/lock;
    2. in one DB transaction: lock the lowest free slot, guarded-update that
       exact slot (must affect 1 row), and write ParameterNo 7 (must affect 1
       row).

    A failed 101/6 never leaves a half-booked slot behind.  If MES succeeds but
    the local booking fails, the transaction rolls back and
    :func:`reconcile_unbooked_positions` repairs the resulting orphan on the
    next cycle.
    """
    # MES4 must not be called while a DB row lock or transaction is open: a
    # slow/timed-out reply would otherwise block MES writes and leave the
    # transaction holding the target slot.  If the local booking fails after
    # MES committed, reconcile_unbooked_positions() repairs the orphan on the
    # next cycle.
    order = mes.create_commissioning_order(
        workplan_number=resource.workplan_number,
        box_id=box_id,
        main_order_number=part.order_number,
        main_order_position=part.order_position,
        main_step_number=part.step_number,
        part_number=part.part_number,
        order_number=order_number,
    )

    with repository.transaction():
        box_position = repository.reserve_free_box_slot(box_id)
        if box_position <= 0:
            raise CapacityError(f"box {box_id} returned invalid position {box_position}")

        repository.book_reserved_box_slot(
            box_id=box_id,
            box_position=box_position,
            order_number=order.order_number,
            order_position=order.order_position,
        )
        repository.book_box_pos_as_target_box_pos_to_order(
            order_number=order.order_number,
            order_position=order.order_position,
            box_position=box_position,
        )

    log.info(
        "Order %s.%s is booked to box %s, position %s",
        order.order_number,
        order.order_position,
        box_id,
        box_position,
    )
    return order


def book_commissioning_orders(
    repository: Repository,
    mes: MesTcp,
    config: Config,
    *,
    policy: SourcePolicy | None = None,
    decisions: DecisionLog | None = None,
) -> None:
    """book-commissioning-orders.ts:12-84.

    Each source slot is one decision: ``policy`` (default :class:`RulePolicy`)
    picks from :meth:`Repository.source_slot_candidates`, the pick is booked,
    and the candidates are updated for the next position of the same part.
    ``decisions`` receives context, candidates and choice of every pick.
    """
    policy = policy or RulePolicy()
    decisions = decisions or NullDecisionLog()
    del mes  # Booking only writes DB rows; MES is not contacted here.
    log.info("")
    log.info("//////////////////////////////////////////")
    log.info("/ Booking parts for commissioning orders /")
    log.info("//////////////////////////////////////////")

    for resource in config.resources:
        log.info("")
        log.info("----------")
        log.info("| %s |", resource.name)
        log.info("----------")
        log.info("")

        try:
            active = repository.active_commissioning_order(resource)
        except PlanningError as exc:
            log.error("Cannot book %s this cycle: %s", resource.name, exc)
            continue
        if active is not None and repository.box_in_disabled_pick_sort_buffer(active.box_id):
            log.error(
                "Target box %s for %s is in a disabled Pick&Sort buffer; "
                "skipping source booking",
                active.box_id,
                resource.name,
            )
            continue

        requested_parts = repository.get_part_requests_for_commissioning_orders_for_resource(
            resource
        )
        if not requested_parts:
            log.info("No parts are required.")
            continue
        log.info("The following parts are required: %s", requested_parts)

        for requested_part in requested_parts:
            candidates = repository.source_slot_candidates(resource, requested_part.part_number)
            # The original SQL selection, kept as a cross-check of the policy.
            legacy = repository.get_box_positions_for_parts_for_resource(
                resource, requested_part.part_number, requested_part.quantity
            )
            if not candidates:
                # FIX: skip this part, but keep serving the other requests.
                log.info(
                    "There is no part %s found in ASRS20; skipping this part request",
                    requested_part.part_number,
                )
                if requested_part.orders:
                    first = requested_part.orders[0]
                    decisions.decision(
                        "source_slot",
                        policy.name,
                        _source_context(resource, requested_part, first, 0),
                        [],
                        None,
                    )
                continue
            log.info(
                "Part %s: %s candidate source slot(s)",
                requested_part.part_number,
                len(candidates),
            )

            picks: list[tuple[int, int]] = []
            for pick, order in enumerate(requested_part.orders):
                if not candidates:
                    break
                context = _source_context(resource, requested_part, order, pick)
                index = policy.choose_source_slot(context, candidates)
                decisions.decision("source_slot", policy.name, context, candidates, index)
                chosen = candidates[index]
                candidates = after_source_pick(candidates, chosen)
                picks.append((chosen.box_id, chosen.box_position))
                position = BoxPosition(box_id=chosen.box_id, box_position=chosen.box_position)

                with repository.transaction():
                    repository.book_order_to_box_position(
                        order_number=order.order_number,
                        order_position=order.order_position,
                        box_id=position.box_id,
                        box_position=position.box_position,
                        part_number=requested_part.part_number,
                    )
                    # Parameters 3 and 4 live in the same transaction as the
                    # source-slot booking.
                    repository.book_box_as_source_box_id_to_order(
                        order_number=order.order_number,
                        order_position=order.order_position,
                        box_id=position.box_id,
                    )
                    repository.book_box_pos_as_source_box_pos_to_order(
                        order_number=order.order_number,
                        order_position=order.order_position,
                        box_position=position.box_position,
                    )

                log.info(
                    "Order %s.%s booked into box position %s.%s",
                    order.order_number,
                    order.order_position,
                    position.box_id,
                    position.box_position,
                )
                log.info(
                    "Box %s is booked as source box id to order %s.%s",
                    position.box_id,
                    order.order_number,
                    order.order_position,
                )

            expected = [(p.box_id, p.box_position) for p in legacy][: len(requested_part.orders)]
            if picks != expected:
                log.warning(
                    "Source policy %s chose %s for part %s at %s; the original SQL selection is %s",
                    policy.name,
                    picks,
                    requested_part.part_number,
                    resource.name,
                    expected,
                )


def _source_context(
    resource: ResourceConfig, request: PartRequest, order: OrderRef, pick: int
) -> SourceSlotContext:
    return SourceSlotContext(
        resource=resource.name,
        workplan=resource.workplan_number,
        part_number=request.part_number,
        order_number=order.order_number,
        order_position=order.order_position,
        requested=request.quantity,
        pick=pick,
    )
