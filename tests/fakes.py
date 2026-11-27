"""Fake repository / MES / Fleet Manager used by the unit tests.

No MariaDB and no network: these classes mirror the small interface the
planner modules consume and keep enough state to verify reservations,
transactions and TCP command sequences.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from logistics_planner.config import Config, load_config
from logistics_planner.decisions import SourceSlotCandidate
from logistics_planner.errors import (
    ConcurrentWriteError,
    MesCommandError,
    MesTcpError,
    QueryRowcountError,
)
from logistics_planner.mes_tcp import MesOrder
from logistics_planner.queries import (
    ActiveCommissioningOrder,
    BoxPosition,
    BoxSlot,
    DirectDemandGroup,
    DisassemblyRequest,
    OrderRef,
    PartDemand,
    PartRequest,
    StaleSourceBooking,
    UnbookedPosition,
)

def load_test_config() -> Config:
    from pathlib import Path

    return load_config(Path(__file__).resolve().parents[1] / "config.json")


def resource_named(config: Config, name: str):
    for resource in config.resources:
        if resource.name == name:
            return resource
    raise KeyError(name)


@dataclass
class FakeMesTcp:
    fail_commissioning: bool = False
    fail_disassembly: bool = False
    fail_locking: bool = False
    next_order_number: int = 900
    # When set, lock/unlock commands update this dict like MES4 updates
    # tblbufferpos.Booked, so a later read in the same cycle sees them.
    lock_states: dict[int, bool] | None = None

    commissioning_calls: list[dict[str, Any]] = field(default_factory=list)
    disassembly_calls: list[dict[str, Any]] = field(default_factory=list)
    buffer_calls: list[tuple[int, bool]] = field(default_factory=list)
    _positions: dict[int, int] = field(default_factory=dict)

    def _allocate(self, order_number: int) -> MesOrder:
        if order_number == 0:
            order_number = self.next_order_number
            self.next_order_number += 1
        position = self._positions.get(order_number, 0) + 1
        self._positions[order_number] = position
        return MesOrder(order_number=order_number, order_position=position)

    def create_commissioning_order(self, **kwargs: Any) -> MesOrder:
        if self.fail_commissioning:
            raise MesTcpError("fake commissioning TCP failure")
        order = self._allocate(kwargs["order_number"])
        self.commissioning_calls.append({**kwargs, "order": order})
        return order

    def create_disassembly_order(self, **kwargs: Any) -> MesOrder:
        if self.fail_disassembly:
            raise MesTcpError("fake disassembly TCP failure")
        order = self._allocate(kwargs["order_number"])
        self.disassembly_calls.append({**kwargs, "order": order})
        return order

    read_only_cycles: int = 0

    def begin_read_only_cycle(self) -> None:
        self.read_only_cycles += 1

    def set_buffer_booked(self, *, resource_id: int, buffer_number: int, locked: bool) -> None:
        if self.fail_locking:
            raise MesCommandError("fake buffer lock failure")
        self.buffer_calls.append((buffer_number, locked))
        if self.lock_states is not None:
            self.lock_states[buffer_number] = locked


@dataclass
class FakeFleet:
    tasks: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None
    calls: int = 0

    def get_task_history(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.tasks


@dataclass
class FakeRepository:
    config: Config
    simulate: bool = False
    read_only: bool = False

    parts: dict[str, list[PartDemand]] = field(default_factory=dict)
    direct_demand_groups: dict[str, list[DirectDemandGroup]] = field(default_factory=dict)
    direct_boxes: dict[int, list[BoxSlot]] = field(default_factory=dict)
    has_store_order: dict[str, bool] = field(default_factory=dict)
    active_orders: dict[str, ActiveCommissioningOrder] = field(default_factory=dict)
    active_errors: dict[str, Exception] = field(default_factory=dict)
    has_commissioning_order: dict[str, bool] | None = None
    has_disassembly_order: dict[str, bool] = field(default_factory=dict)
    buffer_empty: dict[str, bool] = field(default_factory=dict)
    commissioned_at_resource: dict[str, bool] = field(default_factory=dict)
    commissioned_for_disassembly: dict[str, bool] = field(default_factory=dict)
    box_on_way: dict[str, bool] = field(default_factory=dict)

    new_box: int = 0
    boxes: dict[int, list[int]] = field(default_factory=dict)
    disassembly_boxes: dict[str, int] = field(default_factory=dict)

    part_requests: dict[str, list[PartRequest]] = field(default_factory=dict)
    box_positions: dict[tuple[str, int], list[BoxPosition]] = field(default_factory=dict)
    disassembly_requests: dict[str, list[DisassemblyRequest]] = field(default_factory=dict)
    disassembly_requests_without_store: dict[str, list[DisassemblyRequest]] = field(
        default_factory=dict
    )

    positions_to_lock: list[int] = field(default_factory=list)
    lock_states: dict[int, bool] = field(default_factory=dict)
    free_buffers: set[int] = field(default_factory=set)
    disabled_boxes: set[int] = field(default_factory=set)

    unbooked: list[UnbookedPosition] = field(default_factory=list)
    stale_sources: list[StaleSourceBooking] = field(default_factory=list)
    fail_source_reset: bool = False
    released_source_slots: list[tuple[int, int, int, int]] = field(default_factory=list)
    reset_sources: list[tuple[int, int, int, int]] = field(default_factory=list)

    fail_reserve: bool = False
    fail_slot_booking: bool = False
    fail_target_position: bool = False
    fail_source_booking: bool = False
    fail_source_box_id: bool = False
    fail_source_box_position: bool = False

    params7: dict[tuple[int, int], int] = field(default_factory=dict)
    booked_slots: list[tuple[int, int, int, int]] = field(default_factory=list)
    source_bookings: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    source_box_ids: list[tuple[int, int, int]] = field(default_factory=list)
    source_box_positions: list[tuple[int, int, int]] = field(default_factory=list)
    disassembly_queries: list[str] = field(default_factory=list)

    commit_count: int = 0
    rollback_count: int = 0
    simulate_cycle_started: bool = False
    simulate_cycle_rolled_back: bool = False
    _tx_depth: int = 0
    _simulate_snapshot: dict[str, Any] | None = None
    connected_checks: int = 0
    read_only_cycles: int = 0

    def __post_init__(self) -> None:
        if self.has_commissioning_order is None:
            self.has_commissioning_order = {
                resource.name: resource.name in self.active_orders
                for resource in self.config.resources
            }

    # ------------------------------------------------------------ transactions

    def _snapshot(self) -> dict[str, Any]:
        return {
            "boxes": {box_id: list(slots) for box_id, slots in self.boxes.items()},
            "params7": dict(self.params7),
            "booked_slots": list(self.booked_slots),
            "source_bookings": list(self.source_bookings),
            "source_box_ids": list(self.source_box_ids),
            "source_box_positions": list(self.source_box_positions),
            "released_source_slots": list(self.released_source_slots),
            "reset_sources": list(self.reset_sources),
        }

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self.boxes = {box_id: list(slots) for box_id, slots in snapshot["boxes"].items()}
        self.params7 = dict(snapshot["params7"])
        self.booked_slots = list(snapshot["booked_slots"])
        self.source_bookings = list(snapshot["source_bookings"])
        self.source_box_ids = list(snapshot["source_box_ids"])
        self.source_box_positions = list(snapshot["source_box_positions"])
        self.released_source_slots = list(snapshot["released_source_slots"])
        self.reset_sources = list(snapshot["reset_sources"])

    def ensure_connected(self) -> None:
        self.connected_checks += 1

    def begin_read_only_cycle(self) -> None:
        self.read_only_cycles += 1

    @contextmanager
    def transaction(self):
        if self.read_only:
            yield self
            return
        self._tx_depth += 1
        snapshot = self._snapshot()
        try:
            yield self
        except Exception:
            self._restore(snapshot)
            self.rollback_count += 1
            raise
        else:
            self.commit_count += 1
        finally:
            self._tx_depth -= 1

    def begin_simulate_cycle(self) -> None:
        if not self.simulate:
            raise AssertionError("begin_simulate_cycle called without simulate")
        if self.simulate_cycle_started:
            raise AssertionError("simulate cycle already active")
        self.simulate_cycle_started = True
        self.simulate_cycle_rolled_back = False
        self._simulate_snapshot = self._snapshot()

    def end_simulate_cycle(self) -> None:
        if not self.simulate_cycle_started:
            raise AssertionError("end_simulate_cycle without begin")
        assert self._simulate_snapshot is not None
        self._restore(self._simulate_snapshot)
        self.simulate_cycle_started = False
        self.simulate_cycle_rolled_back = True

    # Backwards-compatible names used by older tests.
    def begin_dry_run_cycle(self) -> None:
        self.begin_simulate_cycle()

    def end_dry_run_cycle(self) -> None:
        self.end_simulate_cycle()

    # ------------------------------------------------------------ order checks

    def resource_buffer_has_empty_place(self, resource) -> bool:
        return self.buffer_empty.get(resource.name, True)

    def there_is_a_commissioned_box_at_resource(self, resource) -> bool:
        return self.commissioned_at_resource.get(resource.name, False)

    def there_is_a_commissioned_box_at_resource_for_disassembly(self, resource) -> bool:
        return self.commissioned_for_disassembly.get(resource.name, False)

    def there_is_a_commissioning_order_for_resource(self, resource) -> bool:
        assert self.has_commissioning_order is not None
        return self.has_commissioning_order.get(resource.name, False)

    def there_is_a_disassembly_order_for_resource(self, resource) -> bool:
        return self.has_disassembly_order.get(resource.name, False)

    def there_is_an_open_store_order_for_resource(self, resource) -> bool:
        return self.has_store_order.get(resource.name, False)

    def active_commissioning_order(self, resource) -> ActiveCommissioningOrder | None:
        if resource.name in self.active_errors:
            raise self.active_errors[resource.name]
        return self.active_orders.get(resource.name)

    def is_box_already_on_the_way_to_resource(self, resource, box_id: int) -> bool:
        del box_id
        return self.box_on_way.get(resource.name, False)

    def box_in_disabled_pick_sort_buffer(self, box_id: int) -> bool:
        return box_id in self.disabled_boxes

    def unbooked_commissioning_positions(self) -> list[UnbookedPosition]:
        return list(self.unbooked)

    def stale_source_bookings(self) -> list[StaleSourceBooking]:
        return list(self.stale_sources)

    def release_source_slot(
        self, box_id: int, box_position: int, order_number: int, order_position: int
    ) -> None:
        if self.read_only:
            return
        self.released_source_slots.append((box_id, box_position, order_number, order_position))

    def reset_source_booking(
        self, order_number: int, order_position: int, box_id: int, box_position: int
    ) -> None:
        if self.read_only:
            return
        if self.fail_source_reset:
            raise ConcurrentWriteError("fake guarded parameter 3/4 reset failed")
        self.reset_sources.append((order_number, order_position, box_id, box_position))

    def max_order_number(self) -> int:
        return 1000

    def max_order_position(self, order_number: int) -> int:
        del order_number
        return 0

    # -------------------------------------------------------------- target box

    def box_has_empty_place(self, box_id: int) -> bool:
        return bool(self.boxes.get(box_id))

    def count_free_box_slots(self, box_id: int) -> int:
        return len(self.boxes.get(box_id, []))

    def which_box_to_use_for_new_commissioning_orders(self, number_of_free_positions: int) -> int:
        del number_of_free_positions
        return self.new_box

    def reserve_free_box_slot(self, box_id: int) -> int:
        if not self.read_only and self._tx_depth <= 0:
            raise AssertionError("reserve_free_box_slot must run inside a transaction")
        if self.fail_reserve:
            raise ConcurrentWriteError("fake reservation failure")
        slots = self.boxes.get(box_id, [])
        if not slots:
            from logistics_planner.errors import CapacityError

            raise CapacityError(f"box {box_id} has no free slot")
        if self.read_only:
            return slots[0]
        return slots.pop(0)

    def book_reserved_box_slot(
        self, box_id: int, box_position: int, order_number: int, order_position: int
    ) -> None:
        if self.read_only:
            return
        if self.fail_slot_booking:
            raise ConcurrentWriteError("fake guarded slot update failed")
        self.booked_slots.append((box_id, box_position, order_number, order_position))

    def book_box_pos_as_target_box_pos_to_order(
        self, order_number: int, order_position: int, box_position: int
    ) -> None:
        if self.read_only:
            return
        if self.fail_target_position:
            raise QueryRowcountError("fake ParameterNo 7 update failed")
        if box_position <= 0:
            raise QueryRowcountError("fake refused ParameterNo 7 = 0")
        self.params7[(order_number, order_position)] = box_position

    # ---------------------------------------------------------------- planning

    def which_parts_to_commission_for_resource(self, resource, limit: int) -> list[PartDemand]:
        return list(self.parts.get(resource.name, []))[:limit]

    def direct_delivery_demands(self, resource) -> list[DirectDemandGroup]:
        return list(self.direct_demand_groups.get(resource.name, []))

    def fully_unbooked_asrs_boxes_with_parts(self, part_numbers) -> dict[int, list[BoxSlot]]:
        requested = {int(part_number) for part_number in part_numbers}
        boxes: dict[int, list[BoxSlot]] = {}
        for box_id, slots in self.direct_boxes.items():
            selected = [slot for slot in slots if slot.part_number in requested]
            if selected:
                boxes[box_id] = selected
        return boxes

    def which_disassembly_order_needs_empty_box(
        self, resource, limit: int
    ) -> list[DisassemblyRequest]:
        self.disassembly_queries.append(resource.name)
        return list(self.disassembly_requests.get(resource.name, []))[:limit]

    def which_disassembly_order_needs_empty_box_without_store(
        self, resource, limit: int
    ) -> list[DisassemblyRequest]:
        self.disassembly_queries.append(resource.name)
        return list(self.disassembly_requests_without_store.get(resource.name, []))[:limit]

    def which_box_has_disassembly_order(self, resource, requested_places: int) -> int:
        del requested_places
        return self.disassembly_boxes.get(resource.name, 0)

    # ----------------------------------------------------------------- booking

    def get_part_requests_for_commissioning_orders_for_resource(
        self, resource
    ) -> list[PartRequest]:
        return list(self.part_requests.get(resource.name, []))

    def get_box_positions_for_parts_for_resource(
        self, resource, part_number: int, quantity: int
    ) -> list[BoxPosition]:
        del quantity
        return list(self.box_positions.get((resource.name, part_number), []))

    def source_slot_candidates(self, resource, part_number: int) -> list[SourceSlotCandidate]:
        """``box_positions`` as candidates, in an order RulePolicy keeps.

        Every slot is marked as already used, so the rule orders them by
        ``BoxId, BoxPos``; tests list their positions in that order.
        """
        positions = self.box_positions.get((resource.name, part_number), [])
        return [
            SourceSlotCandidate(
                box_id=p.box_id,
                box_position=p.box_position,
                part_number=part_number,
                used_as_source=True,
                asrs_buf_pos=1,
                free_of_part_in_box=sum(1 for q in positions if q.box_id == p.box_id),
                empty_slots_in_box=0,
                part_slots_in_box=sum(1 for q in positions if q.box_id == p.box_id),
            )
            for p in positions
        ]

    def book_order_to_box_position(
        self,
        order_number: int,
        order_position: int,
        box_id: int,
        box_position: int,
        part_number: int,
    ) -> None:
        if self.read_only:
            return
        if self.fail_source_booking:
            raise ConcurrentWriteError("fake guarded source slot update failed")
        self.source_bookings.append(
            (order_number, order_position, box_id, box_position, part_number)
        )

    def book_box_as_source_box_id_to_order(
        self, order_number: int, order_position: int, box_id: int
    ) -> None:
        if self.read_only:
            return
        if self.fail_source_box_id:
            raise QueryRowcountError("fake ParameterNo 3 update failed")
        self.source_box_ids.append((order_number, order_position, box_id))

    def book_box_pos_as_source_box_pos_to_order(
        self, order_number: int, order_position: int, box_position: int
    ) -> None:
        if self.read_only:
            return
        if self.fail_source_box_position:
            raise QueryRowcountError("fake ParameterNo 4 update failed")
        self.source_box_positions.append((order_number, order_position, box_position))

    # ------------------------------------------------------------ buffer block

    def pick_sort_positions_to_lock(self) -> list[int]:
        return list(self.positions_to_lock)

    def pick_sort_position_is_free(self, position: int) -> bool:
        return position in self.free_buffers

    def pick_sort_lock_states(self) -> dict[int, bool]:
        return dict(self.lock_states)


@dataclass
class FakeCursor:
    connection: "FakeConnection"
    rowcount: int = 0

    def execute(self, sql: str, params: Any = None) -> int:
        self.connection.statements.append((sql, params))
        self.rowcount = 1
        return 1

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self) -> None:
        pass


@dataclass
class FakeConnection:
    statements: list[tuple[str, Any]] = field(default_factory=list)
    begins: int = 0
    commits: int = 0
    rollbacks: int = 0
    pings: int = 0
    autocommit: bool = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def ping(self, reconnect: bool = True) -> None:
        del reconnect
        self.pings += 1

    def begin(self) -> None:
        self.begins += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass
