from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from logistics_planner.config import load_config
from logistics_planner.errors import ConfigError

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def _base() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load(tmp_path: Path, data: dict):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_config(path)


def test_shipped_config_loads_and_resolves_paths():
    config = load_config(CONFIG_PATH)
    assert config.mysql.database == "mes4"
    assert config.mes4.port == 2000
    assert config.mes4.timeout_s == 5.0
    assert config.planner.cycle_s == 5.0
    assert config.planner.buffer_cycle_s == 5.0
    assert Path(config.planner.log_file).is_absolute()
    assert Path(config.planner.lock_file).is_absolute()
    assert config.box.type_pno == 29
    assert config.box.capacity == 8
    assert config.asrs_resource_id == 17
    assert config.pick_and_sort_resource_id == 19
    assert [resource.name for resource in config.resources] == [
        "CP-F-AASS1",
        "CP-F-AASS2",
        "MANDOCK",
        "PALROB",
        "RASS1",
        "RASS2",
        "RASS3",
    ]
    assert all(resource.direct_delivery == "off" for resource in config.resources)


def test_unknown_key_fails_loudly(tmp_path):
    data = _base()
    data["mysql"]["hostn"] = "typo"
    with pytest.raises(ConfigError, match="unknown key"):
        _load(tmp_path, data)


def test_bad_box_capacity_fails_loudly(tmp_path):
    data = _base()
    data["box"]["capacity"] = 0
    with pytest.raises(ConfigError, match="box.capacity"):
        _load(tmp_path, data)


def test_bool_is_not_an_integer(tmp_path):
    data = _base()
    data["mes4"]["port"] = True
    with pytest.raises(ConfigError, match="mes4.port"):
        _load(tmp_path, data)


def test_duplicate_resource_id_fails_loudly(tmp_path):
    data = _base()
    data["resources"][1]["resourceId"] = data["resources"][0]["resourceId"]
    with pytest.raises(ConfigError, match="duplicate resourceId"):
        _load(tmp_path, data)


def test_fleet_manager_requires_http_scheme(tmp_path):
    data = copy.deepcopy(_base())
    data["fleetManager"]["host"] = "localhost:18808"
    with pytest.raises(ConfigError, match="http"):
        _load(tmp_path, data)


def test_buffer_positions_must_fit_the_configured_count(tmp_path):
    data = _base()
    data["pickAndSort"]["disabledBuffers"] = [7]
    with pytest.raises(ConfigError, match="outside 1..6"):
        _load(tmp_path, data)


def test_missing_resources_fails_loudly(tmp_path):
    data = _base()
    data["resources"] = []
    with pytest.raises(ConfigError, match="non-empty"):
        _load(tmp_path, data)


def test_duplicate_disabled_buffers_are_rejected(tmp_path):
    data = _base()
    data["pickAndSort"]["disabledBuffers"] = [2, 2, 6]
    with pytest.raises(ConfigError, match="duplicates"):
        _load(tmp_path, data)


def test_free_buffers_must_fit_non_disabled_buffers(tmp_path):
    data = _base()
    data["pickAndSort"]["numberOfFreeBuffers"] = 5
    data["pickAndSort"]["disabledBuffers"] = [2, 6]
    with pytest.raises(ConfigError, match="not disabled"):
        _load(tmp_path, data)


def test_disabled_buffer_in_priority_list_warns(tmp_path, caplog):
    data = _base()
    data["pickAndSort"]["bufferPosPriorities"] = [2, 4, 1, 3]
    data["pickAndSort"]["disabledBuffers"] = [2, 6]
    with caplog.at_level("WARNING"):
        config = _load(tmp_path, data)
    assert 2 in config.pick_and_sort.disabled_buffers
    assert "disabled buffer 2" in caplog.text


def test_direct_delivery_accepts_off_prefer_and_only(tmp_path):
    for mode in ("off", "prefer", "only"):
        data = _base()
        for resource in data["resources"]:
            if resource["name"] in ("PALROB", "RASS1", "RASS2", "RASS3"):
                resource["directDelivery"] = mode
        config = _load(tmp_path, data)
        modes = {
            resource.name: resource.direct_delivery
            for resource in config.resources
            if resource.name in ("PALROB", "RASS1", "RASS2", "RASS3")
        }
        assert modes == {
            "PALROB": mode,
            "RASS1": mode,
            "RASS2": mode,
            "RASS3": mode,
        }


def test_direct_delivery_rejects_unknown_mode(tmp_path):
    data = _base()
    data["resources"][4]["directDelivery"] = "sometimes"
    with pytest.raises(ConfigError, match="directDelivery"):
        _load(tmp_path, data)


def test_direct_delivery_requires_a_store_workplan(tmp_path):
    # AASS and MANDOCK have workplanDisassemblyNumber 0: no WP 6-9 store
    # workplan exists, so any mode other than off is a configuration error.
    for index in (0, 1, 2):
        data = _base()
        data["resources"][index]["directDelivery"] = "prefer"
        with pytest.raises(ConfigError, match="directDelivery") as excinfo:
            _load(tmp_path, data)
        assert f"resources[{index}]" in str(excinfo.value)
