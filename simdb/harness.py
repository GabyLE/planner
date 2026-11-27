"""Scratch MES4 world: MariaDB server, fake MES4 TCP and fake Fleet Manager.

Shared by the planner integration tests (``tests/integration/conftest.py``)
and the ``simdb`` data-generation CLI.  Nothing here touches the production
database, the real MES4 or the real Fleet Manager.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

try:  # Meant to run with planner/.venv/bin/python.
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only without the planner venv
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[2]
PLANNER_DIR = REPO_ROOT / "planner"
DB_DUMP = REPO_ROOT / "data" / "live_queries" / "mes_db" / "2026_07_02_1731_mes_db_export.sql"
DB_NAME = "mes4"

# Values from the MES4 dump and from the planner configuration contract.
BOX_TYPE_PNO = 29
BOX_CAPACITY = 8
ASRS_RESOURCE_ID = 17
PICK_AND_SORT_RESOURCE_ID = 19

MYSQL_USER = "root"
MYSQL_PASSWORD = ""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"<cannot read {path}>"
    return "\n".join(text[-lines:])


def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def require_pymysql() -> None:
    if pymysql is None:
        raise RuntimeError("pymysql is not installed; run with planner/.venv/bin/python")


# ---------------------------------------------------------------------------
# MariaDB
# ---------------------------------------------------------------------------


class MariaDbServer:
    """A scratch MariaDB server with its own data directory.

    ``port=0`` picks a free port (tests).  The simdb CLI uses a fixed port and
    keeps the server running between invocations; :meth:`stop` then stops it
    through the pid file.
    """

    STARTUP_TIMEOUT_S = 60.0

    def __init__(self, root: Path, *, port: int = 0, db_name: str = DB_NAME) -> None:
        self.root = root
        self.db_name = db_name
        self.datadir = root / "data"
        self.socket_path = root / "mysqld.sock"
        self.pid_file = root / "mysqld.pid"
        self.error_log = root / "mariadb.err"
        self.init_log = root / "mariadb-init.log"
        self.load_log = root / "mariadb-load.log"
        self.host = "127.0.0.1"
        self.port = port
        self._proc: subprocess.Popen[bytes] | None = None

        self.install_db = _which("mariadb-install-db")
        self.server_binary = _which("mariadbd", "mysqld")
        self.client_binary = _which("mariadb", "mysql")

    @property
    def available(self) -> bool:
        return bool(self.install_db and self.server_binary and self.client_binary)

    def connect_kwargs(self, *, database: str | None = "") -> dict[str, Any]:
        assert pymysql is not None
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": MYSQL_USER,
            "password": MYSQL_PASSWORD,
            "charset": "utf8mb4",
            "connect_timeout": 5,
            "read_timeout": 30,
            "write_timeout": 30,
            "autocommit": True,
            "cursorclass": DictCursor,
        }
        if database == "":
            database = self.db_name
        if database is not None:
            kwargs["database"] = database
        return kwargs

    def connect(self, *, database: str | None = "") -> Any:
        assert pymysql is not None
        return pymysql.connect(**self.connect_kwargs(database=database))

    def start(self) -> None:
        if not self.available:
            raise RuntimeError(
                "MariaDB binaries are unavailable (need mariadb-install-db, mariadbd/mysqld "
                "and mariadb/mysql)"
            )

        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.datadir / "mysql").is_dir():
            self.datadir.mkdir(parents=True, exist_ok=True)
            self._install_db()
        self._start_server()

    def _install_db(self) -> None:

        init_cmd = [
            self.install_db,
            "--no-defaults",
            f"--datadir={self.datadir}",
            "--auth-root-authentication-method=normal",
            "--skip-test-db",
            f"--user={_current_user()}",
        ]
        init = subprocess.run(
            init_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            cwd=self.root,
        )
        self.init_log.write_text(
            "COMMAND: " + " ".join(init_cmd) + "\n\nSTDOUT:\n" + init.stdout + "\n\nSTDERR:\n" + init.stderr,
            encoding="utf-8",
        )
        if init.returncode != 0:
            raise RuntimeError(
                f"mariadb-install-db failed with rc={init.returncode}\n"
                f"stdout:\n{init.stdout}\nstderr:\n{init.stderr}"
            )

    def _start_server(self) -> None:
        if self.port == 0:
            self.port = _free_tcp_port()
        cmd = [
            self.server_binary,
            "--no-defaults",
            f"--datadir={self.datadir}",
            f"--socket={self.socket_path}",
            f"--pid-file={self.pid_file}",
            f"--port={self.port}",
            "--bind-address=127.0.0.1",
            "--skip-networking=0",
            "--skip-grant-tables",
            "--skip-name-resolve",
            # Scratch data: no fsync per commit.  On a journaling/CoW home
            # filesystem a single fsync can stall for seconds and trip the
            # planner's MES4 timeout.
            "--innodb-flush-log-at-trx-commit=0",
            "--innodb-doublewrite=0",
            "--sync-binlog=0",
            f"--user={_current_user()}",
            f"--log-error={self.error_log}",
        ]
        log_handle = self.error_log.open("ab")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=self.root,
                start_new_session=True,
            )
        finally:
            log_handle.close()

        try:
            self._wait_until_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.STARTUP_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"mariadbd exited with rc={self._proc.returncode}\n"
                    f"log {self.error_log}:\n{_tail(self.error_log)}"
                )
            try:
                connection = pymysql.connect(
                    host=self.host,
                    port=self.port,
                    user=MYSQL_USER,
                    password=MYSQL_PASSWORD,
                    connect_timeout=1,
                    read_timeout=2,
                    write_timeout=2,
                )
                connection.close()
                return
            except Exception as exc:  # noqa: BLE001 - bounded retry loop
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(
            f"MariaDB did not become ready within {self.STARTUP_TIMEOUT_S:.0f}s "
            f"(last error: {last_error})\nlog {self.error_log}:\n{_tail(self.error_log)}"
        )

    def load_dump(self) -> None:
        """Load the MES4 dump into a fresh database named ``db_name``.

        The dump creates ``mes4`` and uses ``MES4`` (upper case) while the
        Linux server treats database names as case sensitive.  Both
        identifiers are renamed to ``db_name``; the data is untouched.
        """
        if not DB_DUMP.is_file():
            raise RuntimeError(f"MES4 dump not found: {DB_DUMP}")

        name = f"`{self.db_name}`".encode("ascii")
        data = DB_DUMP.read_bytes()
        data = data.replace(b"`MES4`", name).replace(b"`mes4`", name)
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]

        cmd = [
            self.client_binary,
            "--no-defaults",
            "--protocol=TCP",
            f"--host={self.host}",
            f"--port={self.port}",
            f"--user={MYSQL_USER}",
            "--default-character-set=utf8",
        ]
        load = subprocess.run(
            cmd,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            cwd=self.root,
        )
        self.load_log.write_text(
            "COMMAND: " + " ".join(cmd) + "\n\nSTDOUT:\n"
            + load.stdout.decode("utf-8", errors="replace")
            + "\n\nSTDERR:\n"
            + load.stderr.decode("utf-8", errors="replace"),
            encoding="utf-8",
        )
        if load.returncode != 0:
            raise RuntimeError(
                f"loading {DB_DUMP.name} failed with rc={load.returncode}\n"
                f"{_tail(self.load_log)}"
            )

        # Verify that the data actually landed.
        connection = self.connect()
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM tblboxpos")
                box_positions = int(cur.fetchone()["n"])
                cur.execute("SELECT COUNT(*) AS n FROM tbloperationparameter")
                operation_parameters = int(cur.fetchone()["n"])
        finally:
            connection.close()
        if box_positions == 0 or operation_parameters == 0:
            raise RuntimeError(
                "MES4 dump loaded but static tables are empty "
                f"(tblboxpos={box_positions}, tbloperationparameter={operation_parameters})"
            )

    def is_running(self) -> bool:
        """True when a server answers on ``host:port``."""
        if self.port == 0 or pymysql is None:
            return False
        try:
            connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                connect_timeout=1,
            )
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False
        connection.close()
        return True

    def database_exists(self) -> bool:
        connection = self.connect(database=None)
        try:
            with connection.cursor() as cur:
                cur.execute("SHOW DATABASES LIKE %s", (self.db_name,))
                return cur.fetchone() is not None
        finally:
            connection.close()

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            self._stop_by_pid_file()
            return
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def _stop_by_pid_file(self) -> None:
        """Stop a server started by an earlier process (simdb ``up``)."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - extremely unusual environments
        return "root"



class MesDb:
    """Small query helper around one autocommit PyMySQL connection."""

    def __init__(self, server: MariaDbServer) -> None:
        self.server = server
        self.connection = server.connect()

    def close(self) -> None:
        self.connection.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connection.cursor() as cur:
            cur.execute(sql, tuple(params))
            if cur.description is None:
                return []
            return list(cur.fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return next(iter(row.values()))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connection.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.rowcount

    def reset(self, **baseline: Any) -> None:
        """Empty mutable tables and install the controlled baseline world.

        ``baseline`` is passed to :func:`install_baseline`.
        """
        self.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in (
            "tblorder",
            "tblorderpos",
            "tblstep",
            "tblstepparameter",
            "tblboxpos",
            "tblbufferpos",
            "tblboxdef",
        ):
            self.execute(f"TRUNCATE TABLE `{table}`")
        self.execute("SET FOREIGN_KEY_CHECKS=1")
        install_baseline(self, **baseline)


def install_baseline(
    db: MesDb,
    *,
    station_resource_ids: Iterable[int] = (6,),
    asrs_slots: int = 20,
    empty_boxes: Iterable[tuple[int, int]] = ((500, 1), (501, 2)),
) -> None:
    """Box type 29, station buffers, ASRS20 slots, Pick & Sort, empty boxes.

    ``empty_boxes`` are ``(box_id, asrs_buf_pos)`` pairs.  The defaults are
    the integration-test world: only the RASS3 station buffer, twenty ASRS
    slots and empty target boxes 500/501.
    """
    now = datetime.now().replace(microsecond=0)

    # One box type, as in the dump: eight positions per box.
    db.execute(
        "INSERT INTO tblboxdef (BoxPNo, `Count`, PosCount, RowCount) VALUES (%s, %s, %s, %s)",
        (BOX_TYPE_PNO, 40, BOX_CAPACITY, 4),
    )

    # Station buffers, both positions empty.
    for resource_id in station_resource_ids:
        for buf_pos in (1, 2):
            db.execute(
                "INSERT INTO tblbufferpos "
                "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, QuantityMax, "
                " TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
                "VALUES (%s, 1, %s, 0, 0, 0, 12, 7, 0, 0, %s, 0, 0, 7, 0)",
                (resource_id, buf_pos, now),
            )

    # ASRS20 storage: empty buffer slots.  Tests move boxes into these.
    for buf_pos in range(1, asrs_slots + 1):
        db.execute(
            "INSERT INTO tblbufferpos "
            "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, QuantityMax, "
            " TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
            "VALUES (%s, 1, %s, 0, 0, 0, 1, 0, 0, 0, %s, 0, 0, 7, 0)",
            (ASRS_RESOURCE_ID, buf_pos, now),
        )

    # Pick & Sort: six buffers with two positions each.  The permanently
    # disabled buffers start locked, matching the production configuration.
    for buf_no in range(1, 7):
        booked = 1 if buf_no in (2, 6) else 0
        for buf_pos in (1, 2):
            db.execute(
                "INSERT INTO tblbufferpos "
                "(ResourceId, BufNo, BufPos, PNo, ONo, OPos, Type, Zone, Quantity, QuantityMax, "
                " TimeStamp, PalletID, BoxID, PNoGroup, Booked) "
                "VALUES (%s, %s, %s, 0, 0, 0, 12, 7, 0, 0, %s, 0, 0, 7, %s)",
                (PICK_AND_SORT_RESOURCE_ID, buf_no, buf_pos, now, booked),
            )

    # Empty target boxes in ASRS20.  Box 500 is first in the BufPos order.
    for box_id, asrs_pos in empty_boxes:
        for box_pos in range(1, BOX_CAPACITY + 1):
            db.execute(
                "INSERT INTO tblboxpos "
                "(BoxPos, BoxPNo, BoxId, ONo, OPos, PNo, PNoGroup) "
                "VALUES (%s, %s, %s, 0, 0, 0, 9)",
                (box_pos, BOX_TYPE_PNO, box_id),
            )
        db.execute(
            "UPDATE tblbufferpos SET PNo = %s, BoxID = %s, TimeStamp = %s "
            "WHERE ResourceId = %s AND BufNo = 1 AND BufPos = %s",
            (BOX_TYPE_PNO, box_id, now, ASRS_RESOURCE_ID, asrs_pos),
        )



# ---------------------------------------------------------------------------
# Fake MES4 TCP service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MesCommand:
    raw: str
    mclass: int | None
    mno: int | None
    params: dict[str, str]
    received_at: float

    @property
    def string(self) -> str:
        return self.params.get("String", "")

    @property
    def ono(self) -> int | None:
        return _int_or_none(self.params.get("ONo"))

    @property
    def buf_no(self) -> int | None:
        return _int_or_none(self.params.get("BufNo"))

    @property
    def resource_id(self) -> int | None:
        return _int_or_none(self.params.get("ResourceID"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_request(raw: str) -> tuple[int | None, int | None, dict[str, str]]:
    params: dict[str, str] = {}
    for part in raw.replace("\x00", "").strip().split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lstrip("#").strip()
        if key:
            params[key] = value.strip()
    return _int_or_none(params.get("MClass")), _int_or_none(params.get("MNo")), params


def _reply(
    mclass: int | None,
    mno: int | None,
    *,
    error_state: int = 0,
    order_number: int | None = None,
    order_position: int | None = None,
) -> bytes:
    fields = [
        "444",
        "RequestID=0",
        f"MClass={mclass if mclass is not None else 0}",
        f"MNo={mno if mno is not None else 0}",
        f"ErrorState={error_state}",
    ]
    if order_number is not None:
        fields.append(f"ONo={order_number}")
    if order_position is not None:
        fields.append(f"OPos={order_position}")
    return (";".join(fields) + ";\r").encode("ascii")


class FakeMes:
    """Threaded MES4 string-protocol service backed by the test database.

    It implements the subset the planner needs:

    * 101/6 with a nine-field commissioning string -> create/append an order
      position and StepNo-10 parameters 1..11;
    * 101/6 with a three-field disassembly string -> create an order position;
    * 151/15 and 151/16 -> set ``tblbufferpos.Booked`` for the given buffer.

    Requests are recorded in ``commands``.  Each connection may carry several
    requests and each request may arrive in arbitrarily many TCP segments.
    """

    REQUEST_READ_TIMEOUT_S = 15.0

    def __init__(self, server: MariaDbServer, log_path: Path, *, port: int = 0) -> None:
        self.server = server
        self.log_path = log_path
        self.host = "127.0.0.1"
        self.port = port
        self._commands: list[MesCommand] = []
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._tcp_server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.error_log = log_path.with_suffix(".errors.log")

    # ------------------------------------------------------------------ life

    def start(self) -> None:
        require_pymysql()
        owner = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle_connection(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._tcp_server = _Server((self.host, self.port), _Handler)
        self.port = int(self._tcp_server.server_address[1])
        self._thread = threading.Thread(
            target=self._tcp_server.serve_forever,
            name="fake-mes4",
            daemon=True,
        )
        self._thread.start()
        self._log("started fake MES4 on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._log("stopped fake MES4")

    # ------------------------------------------------------------- recording

    @property
    def commands(self) -> list[MesCommand]:
        with self._lock:
            return list(self._commands)

    def _log(self, message: str, *args: Any) -> None:
        text = message % args if args else message
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {text}\n"
        with self._log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _record_error(self, text: str) -> None:
        with self._log_lock:
            self.error_log.parent.mkdir(parents=True, exist_ok=True)
            with self.error_log.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    # -------------------------------------------------------------- transport

    def _handle_connection(self, sock: socket.socket) -> None:
        sock.settimeout(self.REQUEST_READ_TIMEOUT_S)
        buffer = bytearray()
        connection = None
        try:
            connection = self.server.connect()
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    index = _find_terminator(buffer)
                    if index is None:
                        break
                    raw = bytes(buffer[:index]).decode("utf-8", errors="replace")
                    del buffer[: index + 1]
                    if not raw.strip():
                        continue
                    reply = self._process(raw, connection)
                    try:
                        sock.sendall(reply)
                    except OSError:
                        return
        except Exception:  # noqa: BLE001 - keep the server alive for the next connection
            import traceback

            self._record_error(traceback.format_exc())
        finally:
            if connection is not None:
                connection.close()

    def _process(self, raw: str, connection: Any) -> bytes:
        mclass, mno, params = _parse_request(raw)
        command = MesCommand(
            raw=raw,
            mclass=mclass,
            mno=mno,
            params=params,
            received_at=time.time(),
        )
        with self._lock:
            self._commands.append(command)
        self._log("<< %s", raw)

        try:
            if mclass == 101 and mno == 6:
                reply = self._handle_order(command, connection)
            elif mclass == 151 and mno in (15, 16):
                reply = self._handle_buffer_lock(command, connection)
            else:
                self._record_error(f"unsupported command: {raw!r}")
                reply = _reply(mclass, mno, error_state=1)
        except Exception:  # noqa: BLE001 - report ErrorState to the planner
            import traceback

            self._record_error(f"while handling {raw!r}\n{traceback.format_exc()}")
            reply = _reply(mclass, mno, error_state=1)
        self._log(">> %s", reply.decode("ascii", errors="replace").rstrip())
        return reply

    # --------------------------------------------------------------- handlers

    def _handle_order(self, command: MesCommand, connection: Any) -> bytes:
        tokens = command.string.strip().split("-")
        if len(tokens) == 9:
            return self._create_commissioning_order(command, connection, tokens)
        if len(tokens) == 3:
            return self._create_disassembly_order(command, connection, tokens)
        self._record_error(f"unsupported 101/6 string: {command.string!r}")
        return _reply(101, 6, error_state=1)

    def _create_commissioning_order(
        self, command: MesCommand, connection: Any, tokens: list[str]
    ) -> bytes:
        try:
            workplan = int(tokens[0])
            box_id = int(tokens[3])
            main_ono = int(tokens[5])
            main_opos = int(tokens[6])
            main_step = int(tokens[7])
            part_no = int(tokens[8])
        except ValueError:
            self._record_error(f"non-numeric commissioning string: {command.string!r}")
            return _reply(101, 6, error_state=1)

        requested_order = command.ono or 0
        order_number, order_position = self._next_order_slot(connection, requested_order)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO tblorder (ONo, State, Enabled) VALUES (%s, 0, 1) "
                "ON DUPLICATE KEY UPDATE Enabled = 1, State = 0",
                (order_number,),
            )
            cur.execute(
                "INSERT INTO tblorderpos "
                "(ONo, OPos, WPNo, StepNo, State, ResourceID, OpNo, PNo) "
                "VALUES (%s, %s, %s, 10, 0, %s, 220, %s)",
                (order_number, order_position, workplan, PICK_AND_SORT_RESOURCE_ID, part_no),
            )
            # The real MES4 also creates the step row; the planner itself does
            # not strictly need it, but it keeps the fixture realistic.
            cur.execute(
                "INSERT INTO tblstep "
                "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
                " ResourceID, Active) "
                "VALUES (%s, 10, %s, %s, %s, 220, 20, 1, %s, 1)",
                (workplan, order_number, order_position, "commissioning", PICK_AND_SORT_RESOURCE_ID),
            )
            values = {
                1: "0",
                2: "0",
                3: "0",  # source box id (filled by source booking)
                4: "0",  # source box position
                5: "0",
                6: str(box_id),
                7: "0",  # target box position (filled by the planner)
                8: str(main_ono),
                9: str(main_opos),
                10: str(main_step),
                11: str(part_no),
            }
            for parameter_no, value in values.items():
                cur.execute(
                    "INSERT INTO tblstepparameter "
                    "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
                    "VALUES (%s, 10, %s, %s, %s, %s, 2)",
                    (workplan, order_number, order_position, parameter_no, value),
                )

        return _reply(101, 6, order_number=order_number, order_position=order_position)

    def _create_disassembly_order(
        self, command: MesCommand, connection: Any, tokens: list[str]
    ) -> bytes:
        try:
            workplan = int(tokens[0])
            main_ono = int(tokens[1])
            main_opos = int(tokens[2])
        except ValueError:
            self._record_error(f"non-numeric disassembly string: {command.string!r}")
            return _reply(101, 6, error_state=1)

        requested_order = command.ono or 0
        order_number, order_position = self._next_order_slot(connection, requested_order)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO tblorder (ONo, State, Enabled) VALUES (%s, 0, 1) "
                "ON DUPLICATE KEY UPDATE Enabled = 1, State = 0",
                (order_number,),
            )
            cur.execute(
                "INSERT INTO tblorderpos "
                "(ONo, OPos, WPNo, StepNo, State, ResourceID, OpNo, PNo) "
                "VALUES (%s, %s, %s, 10, 0, %s, 220, %s)",
                (order_number, order_position, workplan, PICK_AND_SORT_RESOURCE_ID, workplan),
            )
            cur.execute(
                "INSERT INTO tblstep "
                "(WPNo, StepNo, ONo, OPos, Description, OpNo, NextStepNo, FirstStep, "
                " ResourceID, Active) "
                "VALUES (%s, 10, %s, %s, %s, 220, 20, 1, %s, 1)",
                (workplan, order_number, order_position, "disassembly", PICK_AND_SORT_RESOURCE_ID),
            )
            for parameter_no in range(1, 12):
                cur.execute(
                    "INSERT INTO tblstepparameter "
                    "(WPNo, StepNo, ONo, OPos, ParameterNo, Parameter, ParameterType) "
                    "VALUES (%s, 10, %s, %s, %s, '0', 2)",
                    (workplan, order_number, order_position, parameter_no),
                )

        self._log(
            "disassembly order for main %s.%s requested (unused fields %s)",
            main_ono,
            main_opos,
            command.string,
        )
        return _reply(101, 6, order_number=order_number, order_position=order_position)

    def _handle_buffer_lock(self, command: MesCommand, connection: Any) -> bytes:
        buf_no = command.buf_no
        resource_id = command.resource_id
        buf_pos = _int_or_none(command.params.get("BufPos", "1"))
        if buf_no is None or resource_id is None or buf_pos is None:
            self._record_error(f"151/{command.mno} missing #ResourceID/#BufNo/#BufPos: {command.raw!r}")
            return _reply(151, command.mno, error_state=1)
        booked = 1 if command.mno == 15 else 0
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE tblbufferpos SET Booked = %s "
                "WHERE ResourceId = %s AND BufNo = %s AND BufPos = %s",
                (booked, resource_id, buf_no, buf_pos),
            )
            changed = cur.rowcount
            cur.execute(
                "SELECT COUNT(*) AS n FROM tblbufferpos "
                "WHERE ResourceId = %s AND BufNo = %s AND BufPos = %s",
                (resource_id, buf_no, buf_pos),
            )
            exists = int(cur.fetchone()["n"])
        # UPDATE is idempotent: a lock command for an already locked buffer is
        # still a successful MES4 command.  Only a missing target row is an
        # error.
        if exists != 1:
            self._record_error(
                f"151/{command.mno} target row missing "
                f"(changed={changed}): {command.raw!r}"
            )
            return _reply(151, command.mno, error_state=1)
        return _reply(151, command.mno)

    # ------------------------------------------------------------------ db

    def _next_order_slot(self, connection: Any, requested_order: int) -> tuple[int, int]:
        with connection.cursor() as cur:
            if requested_order == 0:
                cur.execute("SELECT COALESCE(MAX(ONo), 0) + 1 AS nextONo FROM tblorder")
                order_number = int(cur.fetchone()["nextONo"])
                return order_number, 1
            cur.execute(
                "SELECT COALESCE(MAX(OPos), 0) + 1 AS nextOPos FROM tblorderpos WHERE ONo = %s",
                (requested_order,),
            )
            return requested_order, int(cur.fetchone()["nextOPos"])


def _find_terminator(buffer: bytearray) -> int | None:
    indexes = [
        index
        for index in (
            buffer.find(b"*"),
            buffer.find(b"\r"),
            buffer.find(b"\n"),
        )
        if index >= 0
    ]
    return min(indexes) if indexes else None



# ---------------------------------------------------------------------------
# Fake Fleet Manager
# ---------------------------------------------------------------------------


class FakeFleetManager:
    def __init__(self, log_path: Path, *, port: int = 0) -> None:
        self.log_path = log_path
        self.host = "127.0.0.1"
        self.port = port
        self.tasks: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                owner._respond(self)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                owner._respond(self)

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                owner._log("%s %s", self.address_string(), fmt % args)

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="fake-fleet-manager",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _respond(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.requests.append(handler.path)
            body = json.dumps(self.tasks).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _log(self, message: str, *args: Any) -> None:
        text = message % args if args else message
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().isoformat(timespec='milliseconds')} {text}\n")

