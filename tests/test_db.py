from __future__ import annotations

import pytest

from logistics_planner.db import Db

from fakes import FakeConnection, load_test_config


def test_simulate_transaction_uses_savepoint_and_cycle_rollback():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, simulate=True, connection=connection)

    db.begin_simulate_cycle()
    with db.transaction():
        assert db.in_transaction is True
        db.execute("UPDATE tblboxpos SET ONo = %s WHERE BoxId = %s", (1, 11))
    assert db.in_transaction is False

    statements = [sql for sql, _ in connection.statements]
    assert any(sql.startswith("SAVEPOINT planner_sp_") for sql in statements)
    assert any(sql.startswith("RELEASE SAVEPOINT planner_sp_") for sql in statements)
    assert connection.commits == 0
    assert connection.rollbacks == 0  # still inside the cycle-wide transaction

    db.end_simulate_cycle()
    assert connection.rollbacks == 1
    assert connection.commits == 0
    db.close()


def test_failed_simulate_transaction_rolls_back_to_savepoint():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, simulate=True, connection=connection)
    db.begin_simulate_cycle()

    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction():
            db.execute("UPDATE tblboxpos SET ONo = %s WHERE BoxId = %s", (1, 11))
            raise RuntimeError("boom")

    statements = [sql for sql, _ in connection.statements]
    assert any(sql.startswith("ROLLBACK TO SAVEPOINT planner_sp_") for sql in statements)
    assert db.in_transaction is False

    db.end_simulate_cycle()
    assert connection.rollbacks == 1
    db.close()


def test_normal_transaction_commits():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, dry_run=False, connection=connection)

    with db.transaction():
        db.execute("UPDATE tblboxpos SET ONo = %s WHERE BoxId = %s", (1, 11))

    assert connection.begins == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    db.close()


def test_normal_transaction_rolls_back_on_exception():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, dry_run=False, connection=connection)

    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("UPDATE tblboxpos SET ONo = %s WHERE BoxId = %s", (1, 11))
            raise RuntimeError("boom")

    assert connection.begins == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    db.close()


def test_read_only_mode_discards_writes_and_transactions():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, dry_run=True, connection=connection)

    with db.transaction():
        assert db.execute("UPDATE tblboxpos SET ONo = %s WHERE BoxId = %s", (1, 11)) == 1

    assert connection.statements == []
    assert connection.begins == 0
    assert connection.commits == 0
    assert connection.rollbacks == 0
    db.close()


def test_ensure_connected_pings_and_reconnects():
    config = load_test_config()
    connection = FakeConnection()
    db = Db(config, connection=connection)

    db.ensure_connected()
    assert connection.pings == 1
    db.close()


def test_ensure_connected_reconnects_after_ping_failure(monkeypatch):
    config = load_test_config()

    class BrokenConnection(FakeConnection):
        def ping(self, reconnect: bool = True) -> None:
            del reconnect
            raise OSError("connection gone")

    db = Db(config, connection=BrokenConnection())
    reconnects: list[bool] = []
    monkeypatch.setattr(db, "connect", lambda: reconnects.append(True))
    db.ensure_connected()
    assert reconnects == [True]
