"""Booking empty box places for disassembly orders.

Ported from ``book-dissassembly-orders.ts``.  Explicit policy (replacing the
TypeScript code's accidental ``resourceIndex = 0`` jump):

* while **any** commissioning order is active, only the AASS stations
  (``workplanDisassemblyNumber == 0`` and remove op 620) are served;
* otherwise resources are served from the end of the config list to the front;
* one resource is served per cycle;
* every empty-box booking reserves an exact slot with ``FOR UPDATE``, writes it
  with a guarded UPDATE and checks the row count.  For TCP-created disassembly
  positions the 101/6 call is made outside the DB transaction; the following
  booking is still guarded.
"""

from __future__ import annotations

import logging

from .config import Config
from .errors import CapacityError
from .mes_tcp import MesTcp
from .queries import Repository

log = logging.getLogger(__name__)


def _is_aass(resource) -> bool:
    """True for the AASS stations (WPdis 0, remove op 620)."""
    return (
        resource.workplan_disassembly_number == 0
        and tuple(resource.remove_operation_numbers) == (620,)
    )


def book_disassembly_orders(repository: Repository, mes: MesTcp, config: Config) -> None:
    """book-dissassembly-orders.ts:17-165."""
    log.info("")
    log.info("//////////////////////////////////////////////")
    log.info("/ Booking empty place for disassembly orders /")
    log.info("//////////////////////////////////////////////")
    log.info("")

    resources = config.resources

    # Explicit global policy: during an active commissioning campaign only the
    # AASS stations (WPdis 0, remove op 620) get boxes.  MANDOCK also has WPdis
    # 0 but uses manual-boxing op 502; it is not served in this phase.
    has_commissioning_order = {
        resource.name: repository.there_is_a_commissioning_order_for_resource(resource)
        for resource in resources
    }
    commissioning_active = any(has_commissioning_order.values())
    if commissioning_active:
        log.info(
            "A commissioning order is active; only AASS resources (remove op 620) "
            "are served"
        )

    for resource in reversed(resources):
        log.info("")
        log.info("----------")
        log.info("| %s |", resource.name)
        log.info("----------")
        log.info("")

        if commissioning_active and not _is_aass(resource):
            log.info(
                "Commissioning is active; skipping disassembly at %s", resource.name
            )
            continue

        if has_commissioning_order[resource.name]:
            log.info(
                "There is already a commissioning order available for %s", resource.name
            )
            continue
        if repository.there_is_a_disassembly_order_for_resource(resource):
            log.info(
                "There is already a disassembly order available for %s", resource.name
            )
            continue

        # The station buffer must be empty.
        if not repository.resource_buffer_has_empty_place(resource):
            log.info("The buffer of %s is not empty.", resource.name)
            continue

        if repository.there_is_a_commissioned_box_at_resource_for_disassembly(resource):
            log.info("The commissioned box is already at resource")
            if resource.workplan_disassembly_number != 0:
                log.info("Wait till actual disassembly is finished")
                continue

        if resource.workplan_disassembly_number == 0:
            if _book_without_store(repository, config, resource):
                break
        elif _book_with_new_order(repository, mes, config, resource):
            break


def _book_without_store(repository: Repository, config: Config, resource) -> bool:
    """AASS path: book existing disassembly steps into a box without TCP.

    Returns ``True`` when this resource was the one served by the cycle (or
    when planning must stop because no box is available), ``False`` when there
    was simply nothing to do and the next resource should be tried.
    """
    requests = repository.which_disassembly_order_needs_empty_box_without_store(
        resource, config.box.capacity
    )
    if not requests:
        log.info("No empty boxes are required")
        return False
    log.info("The following orders require an empty box for disassembly: %s", requests)

    box_id = repository.which_box_has_disassembly_order(resource, len(requests))
    if box_id == 0:
        log.info("No enough empty place in existing box. Find empty box")
        box_id = repository.which_box_to_use_for_new_commissioning_orders(config.box.capacity)
    else:
        log.info("Box %s is selected as existing box for disassembly orders", box_id)

    if box_id == 0:
        log.info("No empty box available in ASRS20")
        return True
    log.info("Box %s is selected for disassembly orders", box_id)

    free_slots = repository.count_free_box_slots(box_id)
    if free_slots <= 0:
        log.info("Box %s has no free place left; not booking more disassembly orders", box_id)
        return True
    selected = requests[:free_slots]
    if len(selected) < len(requests):
        log.info(
            "Box %s has only %s free place(s); booking %s of %s request(s)",
            box_id,
            free_slots,
            len(selected),
            len(requests),
        )

    for request in selected:
        with repository.transaction():
            box_position = repository.reserve_free_box_slot(box_id)
            if box_position <= 0:
                raise CapacityError(f"box {box_id} returned invalid position {box_position}")
            repository.book_reserved_box_slot(
                box_id=box_id,
                box_position=box_position,
                order_number=request.order_number,
                order_position=request.order_position,
            )
        log.info(
            "Order %s.%s is booked to box %s, position %s",
            request.order_number,
            request.order_position,
            box_id,
            box_position,
        )
    return True


def _book_with_new_order(repository: Repository, mes: MesTcp, config: Config, resource) -> bool:
    """PALROB/RASS path: create disassembly positions via MES 101/6.

    Returns ``True`` when the cycle must stop for this resource (served, or no
    box available), ``False`` when there was nothing to do for it.
    """
    requests = repository.which_disassembly_order_needs_empty_box(
        resource, config.box.capacity
    )
    if not requests:
        log.info("No empty boxes are required")
        return False
    log.info("The following orders require an empty box for disassembly: %s", requests)

    box_id = repository.which_box_to_use_for_new_commissioning_orders(config.box.capacity)
    if box_id == 0:
        log.info("No empty box available in ASRS20")
        return True
    log.info("Box %s is selected as new box for disassembly orders", box_id)

    free_slots = repository.count_free_box_slots(box_id)
    if free_slots <= 0:
        log.info("Box %s has no free place left; not booking more disassembly orders", box_id)
        return True
    selected = requests[:free_slots]
    if len(selected) < len(requests):
        log.info(
            "Box %s has only %s free place(s); booking %s of %s request(s)",
            box_id,
            free_slots,
            len(selected),
            len(requests),
        )

    order_number = 0
    for request in selected:
        # Do not hold a DB transaction/lock across the MES4 TCP call.
        order = mes.create_disassembly_order(
            workplan_disassembly_number=resource.workplan_disassembly_number,
            main_order_number=request.order_number,
            main_order_position=request.order_position,
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
        # Use the same ONo for the next disassembly position of this resource.
        order_number = order.order_number
        log.info(
            "Order %s.%s is booked to box %s, position %s",
            order.order_number,
            order.order_position,
            box_id,
            box_position,
        )
    return True
