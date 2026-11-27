"""Minimal MES4 string-protocol client.

The MES4 TCP service speaks the ``444;Name=Value;...`` string dialect.  For
every command we open a fresh connection (like the TypeScript planner), send
one request and read one reply.  The reply ends with ``*`` or CR/LF; reads can
be split arbitrarily and one ``recv`` can contain more than the reply, so the
reader accumulates until the first terminator.

Reference: ``data/source_code/logistics_planner/logistics_planner_js/src/utils/tcp.ts``
and ``docs/pipeline/mes_tcp.md``.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Any

from .config import Mes4Config
from .errors import MesCommandError, MesTcpError

log = logging.getLogger(__name__)

_TERMINATORS = (b"*", b"\r", b"\n")


@dataclass(frozen=True)
class MesOrder:
    order_number: int
    order_position: int


def _first_terminator(buffer: bytearray) -> int | None:
    indexes = [index for index in (buffer.find(term) for term in _TERMINATORS) if index >= 0]
    return min(indexes) if indexes else None


def _frame_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        text = raw.replace(b"\x00", b"").decode("ascii", errors="replace")
    else:
        text = raw.replace("\x00", "")
    text = text.strip()
    cut = len(text)
    for terminator in ("*", "\r", "\n"):
        index = text.find(terminator)
        if index >= 0:
            cut = min(cut, index)
    return text[:cut].strip()


def parse_fields(text: str) -> dict[str, str]:
    """Parse ``444;A=1;B=2`` into ``{"A": "1", "B": "2"}``.

    Leading ``#`` (request-style header names) and surrounding whitespace are
    removed.  NUL padding is stripped.  A repeated key keeps the last value.
    """
    fields: dict[str, str] = {}
    for part in _frame_text(text).split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lstrip("#").strip()
        if key:
            fields[key] = value.strip()
    return fields


def _check_error_state(fields: dict[str, str], reply: str) -> None:
    if "ErrorState" not in fields:
        raise MesCommandError(f"MES4 reply has no ErrorState: {reply!r}")
    try:
        error_state = int(fields["ErrorState"])
    except ValueError as exc:
        raise MesCommandError(f"MES4 reply has a non-numeric ErrorState: {reply!r}") from exc
    if error_state != 0:
        raise MesCommandError(f"MES4 reported ErrorState={error_state}: {reply!r}")


def parse_order_reply(text: str) -> MesOrder:
    """Return the created order position, or raise on error/malformed replies.

    ``ONo`` is the order number and ``OPos`` the position number.  The
    TypeScript code accidentally destructured the regex groups in the wrong
    order; here the mapping is explicit and tested.  A reply must answer the
    101/6 command; MES4 normally includes both headers, but minimal local test
    replies may omit them.
    """
    fields = parse_fields(text)
    for name, expected in (("MClass", "101"), ("MNo", "6")):
        actual = fields.get(name)
        if actual is not None and actual != expected:
            raise MesCommandError(
                f"MES4 reply does not answer 101/6 ({name}={actual}): {text!r}"
            )
    _check_error_state(fields, text)
    try:
        order_number = int(fields["ONo"])
        order_position = int(fields["OPos"])
    except KeyError as exc:
        raise MesCommandError(f"MES4 reply is missing {exc.args[0]}: {text!r}") from exc
    except ValueError as exc:
        raise MesCommandError(f"MES4 reply has a non-numeric ONo/OPos: {text!r}") from exc
    if order_number <= 0 or order_position <= 0:
        raise MesCommandError(
            f"MES4 reply returned invalid ONo={order_number} OPos={order_position}: {text!r}"
        )
    return MesOrder(order_number=order_number, order_position=order_position)


def _read_reply(sock: socket.socket) -> bytes:
    buffer = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            if buffer:
                raise MesTcpError(
                    "MES4 closed the connection before the reply terminator: "
                    f"{bytes(buffer)!r}"
                )
            raise MesTcpError("MES4 closed the connection without a reply")
        buffer.extend(chunk)
        index = _first_terminator(buffer)
        if index is not None:
            return bytes(buffer[:index])


class MesTcp:
    """MES4 string-protocol client.

    Two shadow modes never open a socket:

    * ``read_only`` (``--dry-run``) logs each command and returns a synthetic
      ONo/OPos for policy flow; no DB rows are simulated;
    * ``simulate`` (``--simulate``) delegates 101/6 to
      ``repository.simulate_mes_*`` so MES rows are visible in the rolled-back
      copy-DB transaction.
    """

    def __init__(
        self,
        config: Mes4Config,
        *,
        read_only: bool = False,
        simulate: bool = False,
        repository: Any = None,
    ) -> None:
        if read_only and simulate:
            raise MesTcpError("--dry-run and --simulate are mutually exclusive")
        self._config = config
        self.read_only = read_only
        self.simulate = simulate
        self._repository = repository
        self._next_order_number: int | None = None
        self._positions: dict[int, int] = {}

    # --------------------------------------------------------------- transport

    def query(self, command: str) -> str:
        if self.read_only or self.simulate:
            raise MesTcpError("raw MES4 queries are not supported in shadow mode")
        log.debug("MES4 >> %s", command)
        try:
            payload = command.encode("ascii")
        except UnicodeEncodeError as exc:  # pragma: no cover - commands are numeric
            raise MesTcpError(f"MES4 command is not ASCII: {command!r}") from exc
        try:
            with socket.create_connection(
                (self._config.host, self._config.port), timeout=self._config.timeout_s
            ) as sock:
                sock.settimeout(self._config.timeout_s)
                sock.sendall(payload)
                raw = _read_reply(sock)
        except socket.timeout as exc:
            raise MesTcpError(
                f"MES4 timeout after {self._config.timeout_s}s "
                f"({self._config.host}:{self._config.port}): {command}"
            ) from exc
        except OSError as exc:
            raise MesTcpError(
                f"MES4 connection failed ({self._config.host}:{self._config.port}): {exc}"
            ) from exc
        reply = raw.decode("ascii", errors="replace")
        log.debug("MES4 << %r", reply)
        return reply

    # --------------------------------------------------------------- commands

    def create_commissioning_order(
        self,
        *,
        workplan_number: int,
        box_id: int,
        main_order_number: int,
        main_order_position: int,
        main_step_number: int,
        part_number: int,
        order_number: int = 0,
    ) -> MesOrder:
        """Send 101/6 for one commissioning position (mes4.ts:491-531)."""
        command = (
            "444;RequestID=0;MClass=101;MNo=6;ErrorState=0;"
            f"#ONo={order_number};"
            f"#String={workplan_number}-0-0-{box_id}-0-"
            f"{main_order_number}-{main_order_position}-{main_step_number}-{part_number}*"
        )
        if self.read_only:
            log.info("[dry-run] would send TCP %s", command)
            return self._read_only_order(order_number)
        if self.simulate:
            log.info("[simulate] TCP %s", command)
            if self._repository is None:
                raise MesTcpError("simulate MES mode requires a repository")
            order_number, order_position = self._repository.simulate_mes_commissioning_order(
                workplan_number=workplan_number,
                box_id=box_id,
                main_order_number=main_order_number,
                main_order_position=main_order_position,
                main_step_number=main_step_number,
                part_number=part_number,
                order_number=order_number,
            )
            return MesOrder(order_number=order_number, order_position=order_position)
        return parse_order_reply(self.query(command))

    def create_disassembly_order(
        self,
        *,
        workplan_disassembly_number: int,
        main_order_number: int,
        main_order_position: int,
        order_number: int = 0,
    ) -> MesOrder:
        """Send 101/6 for a disassembly position (mes4.ts:534-571)."""
        command = (
            "444;RequestID=0;MClass=101;MNo=6;ErrorState=0;"
            f"#ONo={order_number};"
            f"#String={workplan_disassembly_number}-{main_order_number}-{main_order_position}*"
        )
        if self.read_only:
            log.info("[dry-run] would send TCP %s", command)
            return self._read_only_order(order_number)
        if self.simulate:
            log.info("[simulate] TCP %s", command)
            if self._repository is None:
                raise MesTcpError("simulate MES mode requires a repository")
            order_number, order_position = self._repository.simulate_mes_disassembly_order(
                workplan_disassembly_number=workplan_disassembly_number,
                main_order_number=main_order_number,
                main_order_position=main_order_position,
                order_number=order_number,
            )
            return MesOrder(order_number=order_number, order_position=order_position)
        return parse_order_reply(self.query(command))

    def begin_read_only_cycle(self) -> None:
        """Reset the synthetic ONo/OPos counters at the start of a dry-run cycle.

        Every dry-run cycle reads the same unchanged DB, so the same plan must
        get the same synthetic numbers each cycle (MAX(ONo)+1, MAX(OPos)+1)
        instead of counting up forever in a long-running ``run --dry-run``.
        """
        self._next_order_number = None
        self._positions = {}

    def _read_only_order(self, order_number: int) -> MesOrder:
        """Synthetic ONo/OPos for read-only shadow logging."""
        if self._repository is None:
            raise MesTcpError("read-only MES mode requires a repository")
        if order_number == 0:
            if self._next_order_number is None:
                self._next_order_number = self._repository.max_order_number()
            self._next_order_number += 1
            order_number = self._next_order_number
            self._positions.setdefault(order_number, 0)
        if order_number not in self._positions:
            self._positions[order_number] = self._repository.max_order_position(
                order_number
            )
        self._positions[order_number] += 1
        return MesOrder(
            order_number=order_number,
            order_position=self._positions[order_number],
        )

    def set_buffer_booked(self, *, resource_id: int, buffer_number: int, locked: bool) -> None:
        """Send 151/15 (lock) or 151/16 (unlock) for buffer position 1."""
        command_number = 15 if locked else 16
        command = (
            "444;RequestID=0;MClass=151;MNo=%d;ErrorState=0;"
            "#ResourceID=%d;#BufNo=%d;#BufPos=1*"
            % (command_number, resource_id, buffer_number)
        )
        if self.read_only:
            log.info("[dry-run] would send TCP %s", command)
            return
        if self.simulate:
            log.info("[simulate] TCP %s", command)
            return
        reply = self.query(command)
        fields = parse_fields(reply)
        for name, expected in (("MClass", "151"), ("MNo", str(command_number))):
            actual = fields.get(name)
            if actual is not None and actual != expected:
                raise MesCommandError(
                    f"MES4 reply does not answer 151/{command_number} "
                    f"({name}={actual}): {reply!r}"
                )
        _check_error_state(fields, reply)
