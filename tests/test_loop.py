from __future__ import annotations

import pytest

from logistics_planner.errors import LockFileError
from logistics_planner.loop import InstanceLock


def test_instance_lock_is_non_blocking(tmp_path):
    lock_path = tmp_path / "planner.lock"
    with InstanceLock(str(lock_path)):
        with pytest.raises(LockFileError, match="already running"):
            with InstanceLock(str(lock_path)):
                pass
    # After release a new process/instance can take it.
    with InstanceLock(str(lock_path)):
        pass
