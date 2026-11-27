"""Stale source bookings: booked source slot overwritten after booking."""

from __future__ import annotations

from logistics_planner.commissioning import reconcile_stale_source_bookings
from logistics_planner.queries import StaleSourceBooking

from fakes import FakeRepository, load_test_config


def _stale(
    *,
    slot_order: tuple[int, int] | None = (0, 0),
    slot_part: int | None = 814,
    box_position: int = 3,
) -> StaleSourceBooking:
    """The 9219.7 case: box 27 slot 3 booked for 831, later refilled with 814."""
    return StaleSourceBooking(
        order_number=9219,
        order_position=7,
        box_id=27,
        box_position=box_position,
        part_number=831,
        slot_order_number=None if slot_order is None else slot_order[0],
        slot_order_position=None if slot_order is None else slot_order[1],
        slot_part_number=slot_part,
    )


def test_overwritten_slot_resets_parameters_without_touching_slot(caplog):
    repository = FakeRepository(load_test_config(), stale_sources=[_stale()])

    with caplog.at_level("WARNING"):
        reconcile_stale_source_bookings(repository)

    assert repository.reset_sources == [(9219, 7, 27, 3)]
    # The slot no longer carries our reservation, so it must not be cleared:
    # its ONo/OPos may belong to someone else now.
    assert repository.released_source_slots == []
    assert "9219.7" in caplog.text
    assert "PNo 814" in caplog.text
    assert repository.commit_count == 1


def test_still_reserved_wrong_part_releases_slot_then_resets():
    repository = FakeRepository(
        load_test_config(), stale_sources=[_stale(slot_order=(9219, 7), slot_part=814)]
    )

    reconcile_stale_source_bookings(repository)

    assert repository.released_source_slots == [(27, 3, 9219, 7)]
    assert repository.reset_sources == [(9219, 7, 27, 3)]


def test_missing_slot_resets_parameters():
    repository = FakeRepository(
        load_test_config(),
        stale_sources=[_stale(slot_order=None, slot_part=None, box_position=0)],
    )

    reconcile_stale_source_bookings(repository)

    assert repository.released_source_slots == []
    assert repository.reset_sources == [(9219, 7, 27, 0)]


def test_failed_reset_rolls_back_and_reports_once(caplog):
    repository = FakeRepository(
        load_test_config(),
        stale_sources=[_stale(slot_order=(9219, 7))],
        fail_source_reset=True,
    )
    reported: set[tuple[int, int]] = set()

    with caplog.at_level("ERROR"):
        reconcile_stale_source_bookings(repository, reported)
        reconcile_stale_source_bookings(repository, reported)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "9219.7" in errors[0].getMessage()
    assert reported == {(9219, 7)}
    # The slot release in the same transaction was rolled back.
    assert repository.released_source_slots == []
    assert repository.reset_sources == []


def test_reported_key_is_forgotten_once_position_is_fine():
    repository = FakeRepository(load_test_config())
    reported = {(9219, 7)}

    reconcile_stale_source_bookings(repository, reported)

    assert reported == set()


def test_dry_run_logs_but_writes_nothing(caplog):
    repository = FakeRepository(
        load_test_config(), read_only=True, stale_sources=[_stale(slot_order=(9219, 7))]
    )

    with caplog.at_level("WARNING"):
        reconcile_stale_source_bookings(repository)

    assert "[dry-run] would rebook" in caplog.text
    assert repository.released_source_slots == []
    assert repository.reset_sources == []
