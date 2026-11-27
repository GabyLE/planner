"""Planner run loop, single-instance lock and pause flag.

Cycle shape (``run``):

    block (startup) -> plan -> sleep(cycle_s) -> block -> sleep(buffer_cycle_s)

``--once`` runs block + plan immediately and exits.  A failure aborts the
current cycle; the long-running loop logs it and continues with the next one.
SIGINT/SIGTERM set a stop event, so the process exits after the current cycle
instead of in the middle of a transaction.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Callable

from .buffer_block import run_buffer_blocking
from .commissioning import (
    book_commissioning_orders,
    create_commissioning_orders,
    reconcile_stale_source_bookings,
    reconcile_unbooked_positions,
)
from .config import Config
from .decisions import DecisionLog, NullDecisionLog, RulePolicy, SourcePolicy
from .disassembly import book_disassembly_orders
from .errors import LockFileError, PlannerError
from .fleet import FleetManager
from .mes_tcp import MesTcp
from .queries import Repository

log = logging.getLogger(__name__)


class InstanceLock:
    """Non-blocking single-instance file lock (fcntl on POSIX, msvcrt on Windows)."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._handle = None

    def __enter__(self) -> "InstanceLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._path, "a+b")
        except OSError as exc:
            raise LockFileError(f"cannot open lock file {self._path}: {exc}") from exc
        try:
            if os.name == "nt":
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockFileError(
                f"another planner instance is already running "
                f"(lock file {self._path}): {exc}"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            log.exception("releasing lock file %s failed", self._path)
        finally:
            self._handle.close()
            self._handle = None


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _stop(signum, frame) -> None:  # noqa: ARG001 - signal signature
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _stop)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            log.debug("SIGTERM handler could not be installed")


class PlannerRunner:
    def __init__(
        self,
        config: Config,
        repository: Repository,
        mes: MesTcp,
        fleet: FleetManager,
        *,
        decisions: DecisionLog | None = None,
        source_policy: SourcePolicy | None = None,
    ) -> None:
        self._config = config
        self._decisions = decisions or NullDecisionLog()
        self._source_policy = source_policy or RulePolicy()
        self._repository = repository
        self._mes = mes
        self._fleet = fleet
        self._pause_announced = False
        # (ONo, OPos) of orphans already reported; see reconcile_unbooked_positions.
        self._reported_orphans: set[tuple[int, int]] = set()
        # (ONo, OPos) whose stale source booking could not be released.
        self._reported_stale_sources: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ cycles

    def run_plan_cycle(self) -> None:
        log.info("")
        log.info("")
        log.info("#########################################################")
        log.info("# New planning cycle started                            #")
        log.info("#########################################################")
        log.info("")

        self._decisions.begin_cycle()

        def action() -> None:
            reconcile_unbooked_positions(
                self._repository, self._config, self._reported_orphans
            )
            reconcile_stale_source_bookings(self._repository, self._reported_stale_sources)
            create_commissioning_orders(self._repository, self._mes, self._config)
            book_commissioning_orders(
                self._repository,
                self._mes,
                self._config,
                policy=self._source_policy,
                decisions=self._decisions,
            )
            book_disassembly_orders(self._repository, self._mes, self._config)
            log.info("")
            log.info("############################")
            log.info("# planning cycle completed #")
            log.info("############################")
            log.info("")

        self._in_cycle(action)

    def run_block_cycle(self) -> None:
        self._in_cycle(
            lambda: run_buffer_blocking(
                self._repository, self._mes, self._fleet, self._config
            )
        )

    def _in_cycle(self, action: Callable[[], None]) -> None:
        self._repository.ensure_connected()
        if self._repository.read_only:
            # Read-only shadow state (simulated bookings, synthetic ONo/OPos)
            # is per cycle, because each cycle re-reads the unchanged DB.
            self._repository.begin_read_only_cycle()
            self._mes.begin_read_only_cycle()
        simulate = self._repository.simulate
        started = False
        if simulate:
            self._repository.begin_simulate_cycle()
            started = True
        try:
            action()
        finally:
            if started:
                self._repository.end_simulate_cycle()

    # ---------------------------------------------------------------- entry

    def run_once(self) -> None:
        """Run one block + plan cycle and return (or raise)."""
        with InstanceLock(self._config.planner.lock_file):
            # Lock first so disabled/occupied buffers are safe before any
            # commissioning order can be planned.
            self.run_block_cycle()
            self.run_plan_cycle()

    def run_forever(self) -> None:
        stop_event = threading.Event()
        install_signal_handlers(stop_event)
        with InstanceLock(self._config.planner.lock_file):
            log.info("Planner started (cycle %.1fs, buffer cycle %.1fs)",
                     self._config.planner.cycle_s, self._config.planner.buffer_cycle_s)
            # Close the startup gap: enforce buffer locks before the first plan
            # cycle instead of waiting for plan + cycle_s.
            try:
                self.run_block_cycle()
            except PlannerError:
                log.exception("Initial buffer cycle failed; planning will continue")
            except Exception:
                log.exception("Unexpected initial buffer cycle failure")
            while not stop_event.is_set():
                if self._wait_while_paused(stop_event):
                    break
                try:
                    self.run_plan_cycle()
                    self._sleep(stop_event, self._config.planner.cycle_s)
                    if stop_event.is_set():
                        break
                    self.run_block_cycle()
                except PlannerError:
                    log.exception("Planning cycle failed; continuing with the next cycle")
                except Exception:
                    log.exception("Unexpected planning cycle failure; continuing")
                self._sleep(stop_event, self._config.planner.buffer_cycle_s)
            if stop_event.is_set():
                self._log_stop()
            log.info("Planner stopped")

    # --------------------------------------------------------------- helpers

    def _sleep(self, stop_event: threading.Event, seconds: float) -> None:
        stop_event.wait(seconds)

    def _wait_while_paused(self, stop_event: threading.Event) -> bool:
        pause_file = Path(self._config.planner.pause_file)
        if not pause_file.exists():
            self._pause_announced = False
            return False
        if not self._pause_announced:
            log.info("Pause file %s exists; waiting", pause_file)
            self._pause_announced = True
        while pause_file.exists() and not stop_event.is_set():
            stop_event.wait(min(1.0, self._config.planner.cycle_s))
        if stop_event.is_set():
            return True
        log.info("Pause file removed; resuming")
        self._pause_announced = False
        return False

    def _log_stop(self) -> None:
        log.info("Stop requested; exiting after the current cycle")
