"""Source-slot candidates and RulePolicy against the real SQL selection."""

from __future__ import annotations

import json

from simdb import world
from simdb.harness import BOX_TYPE_PNO, PICK_AND_SORT_RESOURCE_ID

PART = 831
RASS3 = world.Resource("RASS3", 6, 3, 325)


def _link_as_source(db, box_id: int, ono: int) -> None:
    """A finished WP3 step-10 position that used ``box_id`` as source for PART."""
    for parameter_no, value in ((3, box_id), (11, PART)):
        db.execute(
            "INSERT INTO tblstepparameter "
            "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
            "VALUES (3, 10, %s, 1, %s, %s, 2)",
            (ono, parameter_no, str(value)),
        )


def test_candidates_and_rule_match_sql_selection(clean_db, planner_env, fake_mes, tmp_path):
    db = clean_db
    used = world.add_box(db, [PART, PART] + [0] * 6)          # already a source
    fullest = world.add_box(db, [PART] * 5 + [0] * 3)        # most free parts
    smaller = world.add_box(db, [PART] * 3 + [0] * 5)
    _link_as_source(db, used, 7000)

    # A used box in disabled Pick & Sort buffer 2 must never be a candidate.
    disabled = 900
    for box_pos in range(1, 9):
        db.execute(
            "INSERT INTO tblboxpos (BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
            "VALUES (%s, %s, %s, 0, 0, %s, 9)",
            (box_pos, BOX_TYPE_PNO, disabled, PART),
        )
    db.execute(
        "UPDATE tblbufferpos SET PNo = %s, BoxID = %s "
        "WHERE ResourceId = %s AND BufNo = 2 AND BufPos = 1",
        (BOX_TYPE_PNO, disabled, PICK_AND_SORT_RESOURCE_ID),
    )
    _link_as_source(db, disabled, 7001)

    world.add_main_order(db, RASS3, [PART] * 4)

    log_path = tmp_path / "decisions.jsonl"
    result = planner_env.run("plan", "--record-decisions", str(log_path))
    assert "the original SQL selection is" not in result.stdout

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    picks = [(r["candidates"][r["chosen"]]["box_id"], r["candidates"][r["chosen"]]["box_position"])
             for r in records]
    assert picks == [(used, 1), (used, 2), (fullest, 1), (fullest, 2)]

    first = records[0]["candidates"]
    assert {c["box_id"] for c in first} == {used, fullest, smaller}
    assert len(first) == 2 + 5 + 3
    assert [c["used_as_source"] for c in first if c["box_id"] == used] == [True, True]
    # Third pick: the used box is exhausted; the fullest box wins over the smaller one.
    third = {c["box_id"]: c for c in records[2]["candidates"]}
    assert third[fullest]["free_of_part_in_box"] == 5 and not third[fullest]["used_as_source"]
    assert third[smaller]["free_of_part_in_box"] == 3
    # Fourth pick: the fullest box is now a source itself.
    assert all(c["used_as_source"] for c in records[3]["candidates"] if c["box_id"] == fullest)

    booked = db.query(
        "SELECT BoxId, BoxPos FROM tblboxpos WHERE PNo = %s AND ONo > 0 ORDER BY BoxId, BoxPos",
        (PART,),
    )
    assert sorted(picks) == sorted((int(r["BoxId"]), int(r["BoxPos"])) for r in booked)
