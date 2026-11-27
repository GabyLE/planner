"""Choice points: candidates, the policy that picks one, and the decision log.

A choice point is split into

1. listing every allowed candidate with the raw facts a policy may use
   (:meth:`Repository.source_slot_candidates`),
2. a policy picking one candidate (:class:`RulePolicy` reproduces the SQL
   ``ORDER BY`` of the TypeScript planner; a learned policy can replace it),
3. updating the remaining candidates for the next pick of the same request
   (:func:`after_source_pick`), without re-reading the DB.  This keeps live,
   ``--dry-run`` and ``--simulate`` identical.

Every pick can be written to a JSONL decision log (:class:`JsonlDecisionLog`,
``--record-decisions PATH``): context, all candidates, the chosen index.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSlotCandidate:
    """One free slot holding the requested part that may become a source.

    ``used_as_source``: the box is already the source (step-10 parameter 3)
    of a position for the same part in the resource's workplan.
    ``asrs_buf_pos``: the box's ASRS20 position, ``None`` when it is not in
    ASRS20 (only a used box may be outside ASRS20).
    """

    box_id: int
    box_position: int
    part_number: int
    used_as_source: bool
    asrs_buf_pos: int | None
    free_of_part_in_box: int
    empty_slots_in_box: int
    part_slots_in_box: int


@dataclass(frozen=True)
class SourceSlotContext:
    resource: str
    workplan: int
    part_number: int
    order_number: int
    order_position: int
    requested: int  # open positions for this part at this resource
    pick: int  # 0-based index of this pick within the request


class SourcePolicy(Protocol):
    name: str

    def choose_source_slot(
        self, context: SourceSlotContext, candidates: Sequence[SourceSlotCandidate]
    ) -> int:
        """Index into ``candidates`` (never empty)."""


class RulePolicy:
    """The TypeScript planner's source rule (``mes4.ts:778-878``).

    Boxes already used as a source for the part come first, by
    ``BoxId, BoxPos``; then ASRS20 boxes with the most free slots of the
    part, by ``BoxId, BoxPos``.
    """

    name = "rule-v1"

    def choose_source_slot(
        self, context: SourceSlotContext, candidates: Sequence[SourceSlotCandidate]
    ) -> int:
        del context

        def key(index: int) -> tuple[int, int, int, int]:
            c = candidates[index]
            if c.used_as_source:
                return (0, 0, c.box_id, c.box_position)
            return (1, -c.free_of_part_in_box, c.box_id, c.box_position)

        return min(range(len(candidates)), key=key)


def after_source_pick(
    candidates: Sequence[SourceSlotCandidate], chosen: SourceSlotCandidate
) -> list[SourceSlotCandidate]:
    """Candidates for the next pick: the slot is gone, its box is now a source."""
    remaining = []
    for c in candidates:
        if (c.box_id, c.box_position) == (chosen.box_id, chosen.box_position):
            continue
        if c.box_id == chosen.box_id:
            c = dataclasses.replace(
                c, used_as_source=True, free_of_part_in_box=c.free_of_part_in_box - 1
            )
        remaining.append(c)
    return remaining


# ---------------------------------------------------------------------------
# decision log
# ---------------------------------------------------------------------------


class DecisionLog(Protocol):
    def begin_cycle(self) -> None: ...

    def decision(
        self, kind: str, policy: str, context: Any, candidates: Sequence[Any], chosen: int | None
    ) -> None: ...


class NullDecisionLog:
    def begin_cycle(self) -> None:
        pass

    def decision(self, kind, policy, context, candidates, chosen) -> None:  # noqa: ANN001
        pass


class JsonlDecisionLog:
    """Append one JSON object per decision to ``path``.

    A write failure is logged and never aborts the planning cycle.
    """

    def __init__(self, path: str | Path, *, mode: str) -> None:
        self.path = Path(path)
        self.mode = mode
        self.run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        self.cycle = -1
        self.seq = 0
        self._warned = False

    def begin_cycle(self) -> None:
        self.cycle += 1
        self.seq = 0

    def decision(
        self, kind: str, policy: str, context: Any, candidates: Sequence[Any], chosen: int | None
    ) -> None:
        record = {
            "ts": time.time(),
            "run_id": self.run_id,
            "cycle": self.cycle,
            "seq": self.seq,
            "kind": kind,
            "mode": self.mode,
            "policy": policy,
            "context": dataclasses.asdict(context),
            "candidates": [dataclasses.asdict(c) for c in candidates],
            "chosen": chosen,
        }
        self.seq += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            if not self._warned:
                log.warning("cannot write decision log %s: %s", self.path, exc)
                self._warned = True
