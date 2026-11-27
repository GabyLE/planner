"""The simdb world model drives the real planner in a closed loop."""

from __future__ import annotations

import json
import random

from simdb import world


def test_closed_loop_books_and_completes_orders(clean_db, planner_env, fake_mes):
    config = json.loads(planner_env.config_path.read_text(encoding="utf-8"))
    resources = world.resources_from_config(config)
    resource_by_wp = {r.workplan: r.name for r in resources}
    parts = list(world.DEFAULT_PARTS)
    rng = random.Random(7)

    clean_db.reset(station_resource_ids=[r.resource_id for r in resources],
                   asrs_slots=40, empty_boxes=())
    world.seed(clean_db, rng, resources, parts, boxes=10, empty_boxes=3, demands=8)

    events: list[dict] = []
    completed = 0
    for _ in range(6):
        before = world.commissioning_positions(clean_db, open_only=False)
        planner_env.run("plan")
        after = world.commissioning_positions(clean_db, open_only=False)
        cycle_events = world.booking_events(before, after, resource_by_wp)
        assert world.check_invariants(clean_db) == []
        events += cycle_events
        completed += world.advance(clean_db, rng, parts)["orders_completed"]
        world.add_demands(clean_db, rng, resources, parts, 2)

    targets = [e for e in events if e["kind"] == "target"]
    sources = [e for e in events if e["kind"] == "source"]
    assert targets and sources and completed > 0
    assert all(e["pos"] > 0 and e["resource"] for e in targets)
    # Every sourced position was created as a target first (same or earlier cycle).
    assert {tuple(e["sub"]) for e in sources} <= {tuple(e["sub"]) for e in targets}
