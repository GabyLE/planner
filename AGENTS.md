# Agent Guidelines — planner

Python 3.10+ replacement for the TypeScript/Node-RED logistics planner.
System documentation: [`../docs/pipeline/logistics_planner.md`](../docs/pipeline/logistics_planner.md).

## Build & test

```bash
./planner/build                  # create .venv, install pymysql+pytest, run tests
./planner/.venv/bin/python -m pytest planner/tests -q
./planner/.venv/bin/python -m pytest planner/tests/integration -q
```

`tests/integration/` uses a throwaway MariaDB loaded from
`data/live_queries/mes_db/2026_07_02_1731_mes_db_export.sql` and a fake MES4
TCP server; it skips when the MariaDB server/client binaries are unavailable.
See [`tests/integration/README.md`](tests/integration/README.md).

## Run

```bash
./planner/run                    # startup block, then plan -> block loop
./planner/run --once             # one block + plan cycle
./planner/run --dry-run          # read-only shadow: log intended writes/TCP
./planner/run --simulate         # copy DB: fake MES rows + rollback (needs copy DB)
./planner/plan [--dry-run|--simulate]   # one planning cycle (reconcile + commission)
./planner/plan --record-decisions decisions.jsonl   # log every source-slot pick + candidates
./planner/block [--dry-run]             # one buffer-locking cycle
./planner/run --help
./planner/.venv/bin/python -m logistics_planner plan|block [--dry-run|--simulate]
```

`--simulate` refuses a database named `mes4` unless
`--i-know-this-is-a-copy` is passed.  `build.bat`, `run.bat`, `plan.bat` and
`block.bat` are the native Windows equivalents.

## Scratch world (decision data)

```bash
./planner/scratch up                       # MariaDB 127.0.0.1:3307, db mes4_sim, .simdb/config.json
./planner/scratch seed --boxes 12 --demands 10
./planner/plan --config planner/.simdb/config.json --dry-run
./planner/scratch loop --reset --cycles 200 --out run.jsonl
./planner/scratch down
```

See "Scratch world and decision data" in the planner doc.

## Config

`planner/config.json` holds MySQL/MES4/Fleet Manager endpoints, loop/log paths,
box capacity, resource ids and the per-resource workplan/operation mapping.
`pickAndSort.disabledBuffers` (currently `[2, 6]`) is always re-locked and is
excluded from source-box selection.  `directDelivery` (`off`/`prefer`/`only`,
default `off`) sends an ASRS20 box that covers a whole main position straight
to the station via the WP 6–9 store workplan; only RASS1–3 and PALROB can
enable it.  Relative `planner.*_file` paths resolve against the config file
directory.
