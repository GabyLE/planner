"""Thin PyMySQL wrapper with explicit transactions and two shadow modes.

* ``read_only`` (``--dry-run``): reads the live DB, logs the writes and MES
  commands it would perform, and never executes a write or opens a
  transaction.  Safe against the production database.
* ``simulate`` (``--simulate``): the older copy-DB mode.  One outer
  transaction spans a cycle, the per-write blocks are savepoints, simulated
  MES rows are inserted, and the whole cycle is rolled back at the end.

Normal operation uses autocommit reads; every guarded write group runs in
``with db.transaction(): ...`` and is committed only when the block succeeds.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from .config import Config
from .errors import DatabaseError

log = logging.getLogger(__name__)

_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _compact(sql: str) -> str:
    return " ".join(sql.split())


class Db:
    def __init__(
        self,
        config: Config,
        *,
        dry_run: bool = False,
        simulate: bool = False,
        connection: Any | None = None,
    ) -> None:
        if dry_run and simulate:
            raise DatabaseError("--dry-run and --simulate are mutually exclusive")
        self._config = config
        self.dry_run = dry_run
        self.simulate = simulate
        self._conn = connection
        self._tx_depth = 0
        self._cycle_active = False
        self._savepoint_seq = 0

    # ------------------------------------------------------------------ setup

    def connect(self) -> None:
        if self._conn is not None:
            return
        mysql = self._config.mysql
        try:
            self._conn = pymysql.connect(
                host=mysql.host,
                port=mysql.port,
                user=mysql.user,
                password=mysql.password,
                database=mysql.database,
                charset=mysql.charset,
                connect_timeout=mysql.connect_timeout,
                autocommit=not self.simulate,
                cursorclass=DictCursor,
            )
        except pymysql.MySQLError as exc:
            raise DatabaseError(
                f"cannot connect to MariaDB {mysql.host}:{mysql.port}/{mysql.database}: {exc}"
            ) from exc
        except OSError as exc:
            raise DatabaseError(
                f"cannot connect to MariaDB {mysql.host}:{mysql.port}/{mysql.database}: {exc}"
            ) from exc
        log.debug("connected to MariaDB %s:%s/%s", mysql.host, mysql.port, mysql.database)

    def ensure_connected(self) -> None:
        """Ping the current connection and reconnect after a DB restart."""
        if self._conn is None:
            self.connect()
            return
        ping = getattr(self._conn, "ping", None)
        if ping is None:  # test doubles/pyMySQL variants without ping
            return
        try:
            ping(reconnect=True)
        except (pymysql.MySQLError, OSError) as exc:
            log.warning("MariaDB connection lost (%s); reconnecting", exc)
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._conn = None
            self.connect()

    def close(self) -> None:
        if self._conn is None:
            return
        if self._cycle_active:
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.exception("rollback of simulate cycle failed during shutdown")
            self._cycle_active = False
        try:
            self._conn.close()
        finally:
            self._conn = None

    def _connection(self) -> Any:
        if self._conn is None:
            raise DatabaseError("database connection is not open")
        return self._conn

    @property
    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    # ------------------------------------------------------------------ query

    def _fetch(self, sql: str, params: tuple[Any, ...], *, one: bool) -> Any:
        connection = self._connection()
        cursor = connection.cursor()
        try:
            log.debug("SQL %s params=%r", _compact(sql), params)
            cursor.execute(sql, params)
            return cursor.fetchone() if one else cursor.fetchall()
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"query failed: {exc}\nSQL: {_compact(sql)} params={params!r}") from exc
        finally:
            cursor.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = self._fetch(sql, params, one=False)
        return list(rows)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return self._fetch(sql, params, one=True)

    def _execute_raw(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Execute without shadow logging (also used for SAVEPOINT statements)."""
        connection = self._connection()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.rowcount
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"write failed: {exc}\nSQL: {_compact(sql)} params={params!r}") from exc
        finally:
            cursor.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        is_write = first in _WRITE_PREFIXES
        if self.dry_run:
            if is_write:
                log.info("[dry-run] would write: %s params=%r", _compact(sql), params)
            # Policy code still needs a row count; nothing is sent to MariaDB.
            return 1
        if self.simulate and is_write:
            log.info("[simulate] write: %s params=%r", _compact(sql), params)
        return self._execute_raw(sql, params)

    # ----------------------------------------------------------- transactions

    def begin_simulate_cycle(self) -> None:
        if not self.simulate:
            return
        if self._cycle_active:
            raise DatabaseError("a simulate cycle transaction is already open")
        self._connection().begin()
        self._cycle_active = True
        self._savepoint_seq = 0
        log.debug("[simulate] cycle transaction started")

    def end_simulate_cycle(self) -> None:
        if not self._cycle_active:
            return
        try:
            self._connection().rollback()
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"[simulate] cycle rollback failed: {exc}") from exc
        finally:
            self._cycle_active = False
        log.debug("[simulate] cycle transaction rolled back")

    def _ensure_cycle_transaction(self) -> None:
        if not self._cycle_active:
            self.begin_simulate_cycle()

    @contextmanager
    def transaction(self) -> Iterator["Db"]:
        """Run a group of writes atomically.

        Read-only shadow mode is a no-op; simulate mode uses a savepoint inside
        the cycle-wide transaction, whose outer rollback still discards
        everything at the end of the cycle.
        """
        if self.dry_run:
            yield self
            return

        connection = self._connection()
        if self.simulate:
            self._ensure_cycle_transaction()
            name = f"planner_sp_{self._savepoint_seq}"
            self._savepoint_seq += 1
            self._execute_raw(f"SAVEPOINT {name}")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                try:
                    self._execute_raw(f"ROLLBACK TO SAVEPOINT {name}")
                except DatabaseError:
                    log.exception("[simulate] rollback to savepoint %s failed", name)
                raise
            else:
                self._execute_raw(f"RELEASE SAVEPOINT {name}")
            finally:
                self._tx_depth -= 1
            return

        connection.begin()
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            self._tx_depth -= 1
