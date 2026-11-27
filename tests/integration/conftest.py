"""Fixtures for the Python logistics-planner MariaDB integration tests.

The tests in this directory exercise the real planner CLI against:

* a throwaway MariaDB instance loaded from the July 2026 MES4 dump,
* a fake MES4 TCP service implementing 101/6 and 151/15/16,
* a fake Fleet Manager REST endpoint.

Nothing here touches the production database, the real MES4 or the real Fleet
Manager.  All ports are ephemeral; every process is started by the fixtures and
torn down again, with its stderr kept in the pytest temporary directory.

The server and fake services live in :mod:`simdb.harness`, which the
``simdb`` data-generation CLI shares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from simdb.harness import (
    ASRS_RESOURCE_ID,
    BOX_CAPACITY,
    BOX_TYPE_PNO,
    DB_DUMP,
    DB_NAME,
    MYSQL_PASSWORD,
    MYSQL_USER,
    PICK_AND_SORT_RESOURCE_ID,
    PLANNER_DIR,
    FakeFleetManager,
    FakeMes,
    MariaDbServer,
    MesDb,
    pymysql,
)


def _require_pymysql() -> None:
    if pymysql is None:
        pytest.skip("pymysql is not installed; run the tests with planner/.venv/bin/python")


@pytest.fixture(scope="session")
def mes_db(tmp_path_factory: pytest.TempPathFactory) -> MariaDbServer:
    """Throwaway MariaDB server loaded from the MES4 dump."""
    _require_pymysql()
    if os.name == "nt":
        pytest.skip("the scratch-MariaDB harness is POSIX-only; use CI/Linux for it")
    root = tmp_path_factory.mktemp("mes-db")
    server = MariaDbServer(root)
    if not server.available:
        pytest.skip(
            "MariaDB binaries are unavailable (need mariadb-install-db, "
            "mariadbd/mysqld and mariadb/mysql)"
        )
    if not DB_DUMP.is_file():
        pytest.fail(f"MES4 dump not found: {DB_DUMP}", pytrace=False)
    try:
        server.start()
        server.load_dump()
    except Exception as exc:  # noqa: BLE001 - report fixture failure with logs
        server.stop()
        pytest.fail(f"could not start/load the scratch MariaDB server: {exc}", pytrace=False)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def clean_db(mes_db: MariaDbServer) -> MesDb:
    """A clean, controlled MES world for one test.

    Static tables from the dump (notably ``tbloperationparameter``) are kept;
    only the mutable planning/booking tables are truncated.
    """
    db = MesDb(mes_db)
    try:
        db.reset()
        yield db
    finally:
        db.close()


@pytest.fixture
def fake_mes(mes_db: MariaDbServer, tmp_path: Path) -> FakeMes:
    """Fake MES4 TCP service bound to the test database."""
    server = FakeMes(mes_db, tmp_path / "fake_mes.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_fleet_manager(tmp_path: Path) -> FakeFleetManager:
    server = FakeFleetManager(tmp_path / "fake_fleet_manager.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Planner CLI
# ---------------------------------------------------------------------------


_RESOURCES = [
    {
        "name": "CP-F-AASS1",
        "resourceId": 26,
        "workplanNumber": 0,
        "workplanDisassemblyNumber": 0,
        "feedOperationNumbers": [0],
        "removeOperationNumbers": [620],
    },
    {
        "name": "CP-F-AASS2",
        "resourceId": 27,
        "workplanNumber": 0,
        "workplanDisassemblyNumber": 0,
        "feedOperationNumbers": [0],
        "removeOperationNumbers": [620],
    },
    {
        "name": "MANDOCK",
        "resourceId": 18,
        "workplanNumber": 5,
        "workplanDisassemblyNumber": 0,
        "feedOperationNumbers": [503],
        "removeOperationNumbers": [502],
    },
    {
        "name": "PALROB",
        "resourceId": 15,
        "workplanNumber": 4,
        "workplanDisassemblyNumber": 9,
        "feedOperationNumbers": [350],
        "removeOperationNumbers": [351],
    },
    {
        "name": "RASS1",
        "resourceId": 2,
        "workplanNumber": 1,
        "workplanDisassemblyNumber": 6,
        "feedOperationNumbers": [323],
        "removeOperationNumbers": [326],
    },
    {
        "name": "RASS2",
        "resourceId": 4,
        "workplanNumber": 2,
        "workplanDisassemblyNumber": 7,
        "feedOperationNumbers": [324],
        "removeOperationNumbers": [327],
    },
    {
        "name": "RASS3",
        "resourceId": 6,
        "workplanNumber": 3,
        "workplanDisassemblyNumber": 8,
        "feedOperationNumbers": [325],
        "removeOperationNumbers": [328],
    },
]


class PlannerEnv:
    """Run the planner CLI from an isolated temporary configuration."""

    def __init__(
        self,
        *,
        root: Path,
        package_parent: Path,
        config_path: Path,
    ) -> None:
        self.root = root
        self.package_parent = package_parent
        self.config_path = config_path
        self._sequence = 0

    def run(
        self,
        *args: str,
        timeout: float = 120.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # The shipped argparse entry point defines --config/dry-run on each
        # subcommand (``python -m logistics_planner plan --config ...``).  Put
        # the subcommand first so the harness also works with that layout; the
        # contract itself lists the same options.
        if args and args[0] in ("plan", "block", "run"):
            command = [
                sys.executable,
                "-m",
                "logistics_planner",
                args[0],
                "--config",
                str(self.config_path),
                *args[1:],
            ]
        else:
            command = [
                sys.executable,
                "-m",
                "logistics_planner",
                "--config",
                str(self.config_path),
                *args,
            ]
        env = os.environ.copy()
        pythonpath = [str(self.package_parent)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        env["PYTHONUNBUFFERED"] = "1"

        self._sequence += 1
        stdout_path = self.root / f"planner-{self._sequence:02d}.out"
        stderr_path = self.root / f"planner-{self._sequence:02d}.err"
        try:
            process = subprocess.run(
                command,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._write_outputs(stdout_path, stderr_path, exc.stdout or "", exc.stderr or "")
            raise AssertionError(
                f"planner command timed out after {timeout}s: {' '.join(command)}\n"
                f"stdout saved to {stdout_path}\nstderr saved to {stderr_path}"
            ) from exc

        self._write_outputs(stdout_path, stderr_path, process.stdout, process.stderr)
        if check and process.returncode != 0:
            raise AssertionError(
                "planner command failed\n"
                f"command: {' '.join(command)}\n"
                f"return code: {process.returncode}\n"
                f"stdout:\n{process.stdout}\n"
                f"stderr:\n{process.stderr}\n"
                f"outputs saved to {stdout_path} / {stderr_path}"
            )
        return process

    @staticmethod
    def _write_outputs(
        stdout_path: Path, stderr_path: Path, stdout: str | bytes, stderr: str | bytes
    ) -> None:
        def text(value: str | bytes) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value or ""

        stdout_path.write_text(text(stdout), encoding="utf-8", errors="replace")
        stderr_path.write_text(text(stderr), encoding="utf-8", errors="replace")


def _write_planner_config(
    path: Path,
    *,
    db: MariaDbServer,
    mes: FakeMes,
    fleet: FakeFleetManager,
    root: Path,
) -> None:
    config = {
        "mysql": {
            "host": db.host,
            "port": db.port,
            "user": MYSQL_USER,
            "password": MYSQL_PASSWORD,
            "database": DB_NAME,
            "charset": "utf8mb4",
            "connect_timeout": 10,
        },
        "mes4": {
            "host": mes.host,
            "port": mes.port,
            "timeout_s": 10,
        },
        "fleetManager": {
            "host": fleet.url,
            "timeout_s": 10,
        },
        "planner": {
            "cycle_s": 1,
            "buffer_cycle_s": 1,
            "lock_file": str(root / "planner.lock"),
            "pause_file": str(root / "planner.pause"),
            "log_file": str(root / "planner.log"),
            "log_max_bytes": 1_048_576,
            "log_backups": 1,
        },
        "box": {
            "type_pno": BOX_TYPE_PNO,
            "capacity": BOX_CAPACITY,
        },
        "asrsResourceId": ASRS_RESOURCE_ID,
        "pickAndSortResourceId": PICK_AND_SORT_RESOURCE_ID,
        "pickAndSort": {
            "numberOfBuffers": 6,
            "numberOfFreeBuffers": 2,
            "bufferPosPriorities": [5, 4, 1, 3],
            "disabledBuffers": [2, 6],
        },
        "resources": _RESOURCES,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def planner_env(
    tmp_path: Path,
    mes_db: MariaDbServer,
    clean_db: MesDb,
    fake_mes: FakeMes,
    fake_fleet_manager: FakeFleetManager,
) -> PlannerEnv:
    """Isolated config/work directory pointing at the test services.

    The core planner is being developed concurrently.  If the package is not
    present yet the tests skip instead of failing collection; once the package
    lands they run against ``python -m logistics_planner``.  The package parent
    can be overridden with ``PLANNER_INTEGRATION_PACKAGE_DIR`` for harness
    self-tests.
    """
    package_parent = Path(
        os.environ.get("PLANNER_INTEGRATION_PACKAGE_DIR", str(PLANNER_DIR))
    ).resolve()
    package_dir = package_parent / "logistics_planner"
    if not package_dir.is_dir() or not (package_dir / "__main__.py").is_file():
        pytest.skip(
            f"planner CLI is not present yet ({package_dir}/__main__.py); "
            "integration tests will run once it lands"
        )

    root = tmp_path / "planner-work"
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.json"
    _write_planner_config(
        config_path,
        db=mes_db,
        mes=fake_mes,
        fleet=fake_fleet_manager,
        root=root,
    )
    return PlannerEnv(
        root=root,
        package_parent=package_parent,
        config_path=config_path,
    )
