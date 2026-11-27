# Planner integration tests (MariaDB + fake MES4)

These tests run the real `python -m logistics_planner plan|block` CLI against a
throwaway MariaDB instance loaded from
`data/live_queries/mes_db/2026_07_02_1731_mes_db_export.sql`, a fake MES4 TCP
service (`101/6`, `151/15`, `151/16`) and a fake Fleet Manager REST endpoint.
They never touch the production database, MES4 or Fleet Manager.

## Prerequisites

* Linux/POSIX with the MariaDB 11.x server/client binaries on `PATH`:
  `mariadb-install-db`, `mariadbd` (or `mysqld`) and `mariadb` (or `mysql`).
  The scratch-server fixture skips itself on Windows; use CI/Linux for it.
* The planner virtual environment from `./planner/build`, which installs
  `pymysql` and `pytest`:
  `planner/.venv/bin/python -c 'import pymysql, pytest'`.
* The MES4 SQL dump at
  `data/live_queries/mes_db/2026_07_02_1731_mes_db_export.sql`.
* Enough free `/tmp` space for a scratch MariaDB data directory (roughly the
  size of the dump plus InnoDB overhead).

If the MariaDB binaries are missing the whole module skips cleanly.  If the
planner package has not been created yet (`planner/logistics_planner/__main__.py`
is absent), the planner-dependent tests skip for the same reason.

## Run

From the repository root:

```bash
planner/.venv/bin/python -m pytest planner/tests/integration -q
```

Focused examples:

```bash
planner/.venv/bin/python -m pytest planner/tests/integration -q -k capacity
planner/.venv/bin/python -m pytest planner/tests/integration -q -k buffer_blocking
```

The same command from inside `planner/` is:

```bash
.venv/bin/python -m pytest tests/integration -q
```

`planner/build` runs `pytest tests -q`, so it already collects this directory.
The explicit command above is useful in CI or when debugging only the
MariaDB/fake-MES path.

## What is covered

* `test_capacity_never_writes_param7_zero` — a box with two free slots and
  five open RASS3 demands gets exactly two commissioning positions, both with
  `StepNo 10, ParameterNo 7` in `1..PosCount`; then the box is completed and
  the remaining three demands are planned into the next empty box.
* `test_repeated_cycles_no_double_booking_or_param7_zero` — several `plan`
  cycles (before and after completing a box, plus after resetting the scratch
  DB) keep the invariants: no zero target position, no shared target slot, no
  box above `tblboxdef.PosCount`, and every new commissioning position has a
  matching target `tblboxpos` row.
* `test_buffer_blocking_maps_bufno` — `block` maps `151/15` and `151/16`
  requests by actual `BufNo` (including non-contiguous buffer 3), keeps
  disabled buffers 2 and 6 locked, keeps Robotino source/target buffers
  locked, and releases at most two non-disabled buffers.
* `test_disassembly_without_store_books_an_empty_slot` — an AASS remove step
  (op 620) takes the no-TCP path, reserves one real target slot and creates
  no MES4 order.
* `test_disassembly_creates_a_tcp_order_and_books_a_slot` — a PALROB remove
  step sends the three-field 101/6 string and books the returned ONo/OPos.
* `test_disassembly_reuses_existing_box_up_to_poscount` — a partly used
  disassembly box is reused while `PosCount - used` has room; two requests
  fill its last two slots.
* `test_direct_delivery_stores_a_covering_asrs_box_without_pick_and_sort` —
  with RASS1 set to `prefer`, a fully unbooked ASRS20 box holding 800 + 814
  creates one WP 6 store order and books both slots to the returned
  sub-order; no WP 1 Pick & Sort order is created.
* `test_ambiguous_active_orders_skip_resource` — two active commissioning
  boxes/orders for RASS3 make the planner log and skip that resource instead
  of guessing or creating a third order.
* `test_disabled_buffer_is_relocked_when_fleet_manager_is_down` — an
  operator/MES reset of BufNo 2 to `Booked=0` is repaired by the lock pass
  even though the Fleet Manager endpoint is stopped; no unlock is sent.
* `test_source_box_in_disabled_buffer_is_not_chosen` — a preferred source box
  sitting in BufNo 2 is ignored; the ASRS copy is used instead.
* `test_planted_orphan_position_is_reconciled` — an old `ParameterNo 7 = 0`
  position with no target slot is booked and repaired on the next plan.
* `test_simulate_rolls_back_plan_and_does_not_send_tcp` — `--simulate
  --i-know-this-is-a-copy` inserts fake MES rows, leaves the DB unchanged
  after rollback and sends no 101/6 frame.
* `test_read_only_dry_run_changes_nothing` — `--dry-run` changes no rows or
  buffer states, sends no 101/6 or 151 frame, and leaves no open InnoDB
  transaction.

## Implementation notes

* All ports are allocated by the OS (`127.0.0.1:0`); no fixed MES4, MariaDB or
  Fleet Manager port is required.
* The MariaDB data directory, socket, pid file and error log live in the
  pytest temporary directory.  `mariadbd` is started with
  `--skip-grant-tables` for passwordless root access and is terminated during
  fixture teardown.
* The fake MES4 server is threaded, accepts sequential connections and handles
  split/coalesced TCP frames.  Its request log is written next to the pytest
  temp files (`fake_mes.log`); handler failures go to `fake_mes.errors.log`.
* Planner stdout/stderr and its rotating log file are also written to the
  temporary planner work directory, so a failed CLI run is easy to inspect.
* `clean_db` truncates only the mutable planning/booking tables and keeps the
  static tables from the dump, notably `tbloperationparameter`.
