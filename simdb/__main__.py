"""simdb: run the real planner against a scratch MES4 world you control.

    planner/scratch up                 # start MariaDB (port 3307), load dump, write config
    planner/scratch reset              # empty world: buffers, ASRS20 slots, no boxes/orders
    planner/scratch seed --boxes 12 --demands 10
    planner/plan --config planner/.simdb/config.json --dry-run    # one decision, nothing written
    planner/scratch serve              # fake MES4 + Fleet Manager for non-dry-run planner runs
    planner/scratch loop --cycles 200 --out decisions.jsonl     # closed loop, one JSON line per cycle
    planner/scratch down

Everything lives in ``planner/.simdb/`` (``--workdir``).  The generated
config points only at 127.0.0.1 ports owned by this tool.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import world
from .harness import DB_DUMP, PLANNER_DIR, FakeFleetManager, FakeMes, MariaDbServer, MesDb

DEFAULT_WORKDIR = PLANNER_DIR / ".simdb"
TEMPLATE_CONFIG = PLANNER_DIR / "config.json"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.config_path = workdir / "config.json"

    @property
    def config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            sys.exit(f"simdb: {self.config_path} missing; run 'simdb up' first")
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def server(self) -> MariaDbServer:
        mysql = self.config["mysql"]
        return MariaDbServer(self.workdir / "mariadb", port=int(mysql["port"]),
                             db_name=mysql["database"])

    def db(self) -> MesDb:
        server = self.server()
        if not server.is_running():
            sys.exit(f"simdb: no MariaDB on port {server.port}; run 'simdb up'")
        return MesDb(server)

    def resources(self) -> list[world.Resource]:
        return world.resources_from_config(self.config)

    def station_resource_ids(self) -> list[int]:
        return [r.resource_id for r in self.resources()]

    def fakes(self) -> tuple[FakeMes, FakeFleetManager]:
        config = self.config
        fleet_port = int(config["fleetManager"]["host"].rsplit(":", 1)[1])
        mes = FakeMes(self.server(), self.workdir / "fake_mes.log", port=int(config["mes4"]["port"]))
        fleet = FakeFleetManager(self.workdir / "fake_fleet.log", port=fleet_port)
        return mes, fleet


def write_config(env: Env, *, db_port: int, db_name: str, mes_port: int, fleet_port: int) -> None:
    """Production config.json with every endpoint replaced by a local one."""
    config = json.loads(TEMPLATE_CONFIG.read_text(encoding="utf-8"))
    config["mysql"].update(host="127.0.0.1", port=db_port, user="root", password="",
                           database=db_name, charset="utf8mb4")
    config["mes4"].update(host="127.0.0.1", port=mes_port)
    config["fleetManager"].update(host=f"http://127.0.0.1:{fleet_port}")
    config["planner"].update(lock_file="planner.lock", pause_file="planner.pause",
                             log_file="planner.log")
    env.workdir.mkdir(parents=True, exist_ok=True)
    env.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def parse_parts(text: str) -> list[int]:
    return [int(p) for p in text.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_up(env: Env, args: argparse.Namespace) -> None:
    if args.db == "mes4":
        sys.exit("simdb: use a database name other than 'mes4' so it can never be the live one")
    write_config(env, db_port=args.port, db_name=args.db, mes_port=args.mes_port,
                 fleet_port=args.fleet_port)
    server = env.server()
    if server.is_running():
        print(f"MariaDB already running on 127.0.0.1:{server.port}")
    else:
        server.start()
        print(f"MariaDB started on 127.0.0.1:{server.port} (data {server.datadir})")
    if not server.database_exists():
        print(f"loading {DB_DUMP.name} into {args.db} ...")
        server.load_dump()
        db = MesDb(server)
        db.reset(station_resource_ids=env.station_resource_ids(), asrs_slots=args.asrs_slots,
                 empty_boxes=())
        db.close()
    print(f"planner config: {env.config_path}")


def cmd_down(env: Env, args: argparse.Namespace) -> None:
    env.server().stop()
    print("MariaDB stopped")


def cmd_reset(env: Env, args: argparse.Namespace) -> None:
    db = env.db()
    db.reset(station_resource_ids=env.station_resource_ids(), asrs_slots=args.asrs_slots,
             empty_boxes=())
    db.close()
    print(f"world reset: {args.asrs_slots} empty ASRS20 slots, no boxes, no orders")


def cmd_seed(env: Env, args: argparse.Namespace) -> None:
    db = env.db()
    rng = random.Random(args.seed)
    print(json.dumps(world.seed(db, rng, env.resources(), parse_parts(args.parts),
                                boxes=args.boxes, empty_boxes=args.empty_boxes,
                                demands=args.demands)))
    db.close()


def cmd_advance(env: Env, args: argparse.Namespace) -> None:
    db = env.db()
    rng = random.Random(args.seed)
    print(json.dumps(world.advance(db, rng, parse_parts(args.parts), p_complete=args.p_complete,
                                   refill=not args.no_refill)))
    db.close()


def cmd_state(env: Env, args: argparse.Namespace) -> None:
    db = env.db()
    print(json.dumps(world.snapshot(db), indent=1))
    db.close()


def cmd_serve(env: Env, args: argparse.Namespace) -> None:
    env.db().close()  # fail early without a database
    mes, fleet = env.fakes()
    mes.start()
    fleet.start()
    print(f"fake MES4 on 127.0.0.1:{mes.port}, fake Fleet Manager on {fleet.url}; Ctrl-C stops")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mes.stop()
        fleet.stop()


def run_planner(env: Env, command: str, extra: list[str]) -> subprocess.CompletedProcess[str]:
    python = PLANNER_DIR / ".venv" / "bin" / "python"
    return subprocess.run(
        [str(python if python.exists() else sys.executable), "-m", "logistics_planner", command,
         "--config", str(env.config_path), *extra],
        cwd=PLANNER_DIR, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def cmd_loop(env: Env, args: argparse.Namespace) -> None:
    db = env.db()
    rng = random.Random(args.seed)
    parts = parse_parts(args.parts)
    resources = env.resources()
    resource_by_wp = {r.workplan: r.name for r in resources}
    if args.reset:
        db.reset(station_resource_ids=[r.resource_id for r in resources],
                 asrs_slots=args.asrs_slots, empty_boxes=())
        world.seed(db, rng, resources, parts, boxes=args.boxes, empty_boxes=args.empty_boxes,
                   demands=args.demands)

    mes, fleet = env.fakes()
    mes.start()
    fleet.start()
    out = open(args.out, "a", encoding="utf-8")
    totals = {"cycles": 0, "events": 0, "decisions": 0, "failed_cycles": 0}
    try:
        for cycle in range(args.cycles):
            arrivals = _poisson(rng, args.arrivals)
            if arrivals:
                world.add_demands(db, rng, resources, parts, arrivals)
            state = world.snapshot(db)
            before = world.commissioning_positions(db, open_only=False)
            decision_file = env.workdir / "cycle_decisions.jsonl"
            decision_file.unlink(missing_ok=True)
            result = run_planner(env, "plan", ["--record-decisions", str(decision_file)])
            decisions = _read_jsonl(decision_file)
            after = world.commissioning_positions(db, open_only=False)
            events = world.booking_events(before, after, resource_by_wp)
            problems = world.check_invariants(db)
            effects = world.advance(db, rng, parts, p_complete=args.p_complete,
                                    refill=not args.no_refill)
            record = {
                "cycle": cycle, "seed": args.seed, "arrivals": arrivals,
                "planner_rc": result.returncode, "state": state, "events": events,
                "decisions": decisions,
                "effects": effects, "problems": problems,
            }
            mismatches = [line for line in result.stdout.splitlines()
                          if "the original SQL selection is" in line]
            if mismatches:
                record["policy_mismatches"] = mismatches
                problems.extend(mismatches)
            if result.returncode != 0:
                record["planner_stderr"] = result.stderr[-4000:]
                totals["failed_cycles"] += 1
            out.write(json.dumps(record) + "\n")
            out.flush()
            totals["cycles"] += 1
            totals["events"] += len(events)
            totals["decisions"] += len(decisions)
            if problems or result.returncode != 0:
                print(f"cycle {cycle}: rc={result.returncode} problems={problems}", file=sys.stderr)
                if args.stop_on_error:
                    break
            if args.verbose:
                print(f"cycle {cycle}: {len(events)} events, {effects}")
    finally:
        out.close()
        mes.stop()
        fleet.stop()
        db.close()
    print(json.dumps({**totals, "out": str(args.out)}))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    # Knuth; lam is small (demands per cycle).
    limit, k, p = pow(2.718281828459045, -lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="simdb", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    sub = parser.add_subparsers(dest="command", required=True)

    def world_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--parts", default=",".join(map(str, world.DEFAULT_PARTS)),
                       help="comma-separated part numbers")

    p = sub.add_parser("up", help="start MariaDB, load the dump once, write config.json")
    p.add_argument("--port", type=int, default=3307)
    p.add_argument("--db", default="mes4_sim")
    p.add_argument("--mes-port", type=int, default=2001)
    p.add_argument("--fleet-port", type=int, default=18809)
    p.add_argument("--asrs-slots", type=int, default=20)
    p.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop MariaDB").set_defaults(func=cmd_down)

    p = sub.add_parser("reset", help="empty world (static tables from the dump are kept)")
    p.add_argument("--asrs-slots", type=int, default=20)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("seed", help="add random part boxes, empty boxes and demands")
    world_flags(p)
    p.add_argument("--boxes", type=int, default=12)
    p.add_argument("--empty-boxes", type=int, default=3)
    p.add_argument("--demands", type=int, default=10)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("advance", help="apply MES/station effects to the planner's bookings")
    world_flags(p)
    p.add_argument("--p-complete", type=float, default=1.0)
    p.add_argument("--no-refill", action="store_true")
    p.set_defaults(func=cmd_advance)

    sub.add_parser("state", help="print the state the planner reads").set_defaults(func=cmd_state)
    sub.add_parser("serve", help="run fake MES4 + Fleet Manager in the foreground").set_defaults(
        func=cmd_serve)

    p = sub.add_parser("loop", help="closed loop: demands -> planner plan -> record -> advance")
    world_flags(p)
    p.add_argument("--cycles", type=int, default=50)
    p.add_argument("--out", type=Path, default=DEFAULT_WORKDIR / "decisions.jsonl")
    p.add_argument("--arrivals", type=float, default=2.0, help="mean new demands per cycle")
    p.add_argument("--p-complete", type=float, default=0.7,
                   help="chance a fully booked order is executed per cycle")
    p.add_argument("--no-refill", action="store_true")
    p.add_argument("--no-refill", action="store_true")
    p.add_argument("--reset", action="store_true", help="reset + seed before the first cycle")
    p.add_argument("--asrs-slots", type=int, default=20)
    p.add_argument("--boxes", type=int, default=12)
    p.add_argument("--empty-boxes", type=int, default=3)
    p.add_argument("--demands", type=int, default=10)
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_loop)

    args = parser.parse_args(argv)
    args.func(Env(args.workdir.resolve()), args)


if __name__ == "__main__":
    main()
