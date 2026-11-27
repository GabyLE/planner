from __future__ import annotations

import socket
import threading
import time

import pytest

from logistics_planner.config import Mes4Config
from logistics_planner.errors import MesCommandError, MesTcpError
from logistics_planner.mes_tcp import MesTcp, parse_fields, parse_order_reply


class OneShotServer:
    """Accept one connection, read the command, then send ``chunks``."""

    def __init__(self, chunks: list[bytes], *, read_until: bytes = b"*", delay: float = 0.01):
        self.chunks = chunks
        self.read_until = read_until
        self.delay = delay
        self.received = b""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            connection, _ = self._socket.accept()
            with connection:
                buffer = b""
                while self.read_until not in buffer:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                self.received = buffer
                for chunk in self.chunks:
                    connection.sendall(chunk)
                    time.sleep(self.delay)
        except OSError:
            pass
        finally:
            self._socket.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._thread.join(timeout=2)
        self._socket.close()


def _mes(server: OneShotServer) -> MesTcp:
    return MesTcp(Mes4Config(host="127.0.0.1", port=server.port, timeout_s=1.0))


def _create_commissioning(mes: MesTcp):
    return mes.create_commissioning_order(
        workplan_number=3,
        box_id=11,
        main_order_number=100,
        main_order_position=2,
        main_step_number=10,
        part_number=831,
    )


def test_parses_split_cr_terminated_reply_and_command():
    chunks = [
        b"444;RequestID=1;MClass=101;",
        b"MNo=6;ErrorState=0;",
        b"\x00ONo=321;OPos=7;\r",
        b"trailing garbage",
    ]
    with OneShotServer(chunks) as server:
        order = _create_commissioning(_mes(server))
    assert (order.order_number, order.order_position) == (321, 7)
    assert (
        b"#String=3-0-0-11-0-100-2-10-831*" in server.received
    ), server.received
    assert b"#ONo=0;" in server.received


def test_star_terminator_and_nul_padding():
    with OneShotServer([b"444;ErrorState=0;ONo=5;OPos=2;*\x00"]) as server:
        order = _create_commissioning(_mes(server))
    assert (order.order_number, order.order_position) == (5, 2)


def test_coalesced_replies_use_the_first_frame():
    first = b"444;ErrorState=0;ONo=11;OPos=3;\r"
    second = b"444;ErrorState=0;ONo=12;OPos=4;\r"
    with OneShotServer([first + second]) as server:
        order = _create_commissioning(_mes(server))
    assert (order.order_number, order.order_position) == (11, 3)


def test_error_state_raises_command_error():
    with OneShotServer([b"444;ErrorState=5;ONo=1;OPos=1;\r"]) as server:
        with pytest.raises(MesCommandError, match="ErrorState=5"):
            _create_commissioning(_mes(server))


def test_missing_ono_raises_command_error():
    with OneShotServer([b"444;ErrorState=0;OPos=1;\r"]) as server:
        with pytest.raises(MesCommandError, match="ONo"):
            _create_commissioning(_mes(server))


def test_parse_fields_strips_hash_and_accepts_equals_in_values():
    fields = parse_fields("#ONo=9;#String=a=b;ErrorState=0;*")
    assert fields["ONo"] == "9"
    assert fields["String"] == "a=b"
    assert fields["ErrorState"] == "0"


def test_parse_order_reply_maps_ono_and_opos_correctly():
    order = parse_order_reply("444;ErrorState=0;ONo=123;OPos=4;\r")
    assert order.order_number == 123
    assert order.order_position == 4


def test_parse_order_reply_rejects_zero_position():
    with pytest.raises(MesCommandError, match="invalid"):
        parse_order_reply("444;ErrorState=0;ONo=123;OPos=0;\r")


def test_parse_order_reply_rejects_a_reply_for_another_command():
    with pytest.raises(MesCommandError, match="101/6"):
        parse_order_reply("444;MClass=151;MNo=15;ErrorState=0;ONo=123;OPos=4;\r")


def test_timeout_raises_tcp_error():
    class SilentServer:
        def __init__(self):
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(1)
            self.port = self._socket.getsockname()[1]
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

        def _run(self):
            try:
                connection, _ = self._socket.accept()
                with connection:
                    connection.recv(4096)
                    time.sleep(0.5)
            except OSError:
                pass

        def close(self):
            self._socket.close()

    server = SilentServer()
    try:
        mes = MesTcp(Mes4Config(host="127.0.0.1", port=server.port, timeout_s=0.1))
        with pytest.raises(MesTcpError, match="timeout"):
            _create_commissioning(mes)
    finally:
        server.close()
