"""Configuration loading and validation.

The JSON layout is the ported TypeScript ``config.json`` plus the planner
run-time keys (TCP host, Fleet Manager, loop and logging settings).  All
validation happens while loading: a typo or an out-of-range value raises
:class:`~logistics_planner.errors.ConfigError` instead of being silently
defaulted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

log = logging.getLogger(__name__)

_MISSING = object()


@dataclass(frozen=True)
class MySqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str
    connect_timeout: int


@dataclass(frozen=True)
class Mes4Config:
    host: str
    port: int
    timeout_s: float


@dataclass(frozen=True)
class FleetManagerConfig:
    host: str
    timeout_s: float


@dataclass(frozen=True)
class PlannerSettings:
    cycle_s: float
    buffer_cycle_s: float
    lock_file: str
    pause_file: str
    log_file: str
    log_max_bytes: int
    log_backups: int


@dataclass(frozen=True)
class BoxConfig:
    type_pno: int
    capacity: int


@dataclass(frozen=True)
class PickAndSortConfig:
    number_of_buffers: int
    number_of_free_buffers: int
    buffer_pos_priorities: tuple[int, ...]
    disabled_buffers: tuple[int, ...]


_DIRECT_DELIVERY_MODES = frozenset({"off", "prefer", "only"})


@dataclass(frozen=True)
class ResourceConfig:
    name: str
    resource_id: int
    workplan_number: int
    workplan_disassembly_number: int
    feed_operation_numbers: tuple[int, ...]
    remove_operation_numbers: tuple[int, ...]
    direct_delivery: str = "off"


@dataclass(frozen=True)
class Config:
    mysql: MySqlConfig
    mes4: Mes4Config
    fleet_manager: FleetManagerConfig
    planner: PlannerSettings
    box: BoxConfig
    asrs_resource_id: int
    pick_and_sort_resource_id: int
    pick_and_sort: PickAndSortConfig
    resources: tuple[ResourceConfig, ...]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object, got {type(value).__name__}")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")


def _get(data: Mapping[str, Any], key: str, path: str, default: Any = _MISSING) -> Any:
    if key in data:
        return data[key]
    if default is not _MISSING:
        return default
    raise ConfigError(f"{path}: missing required key '{key}'")


def _as_int(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    # bool is an int subclass; reject it so ``true`` never becomes 1 silently.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{path} must be <= {maximum}, got {value}")
    return value


def _as_float(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number, got {type(value).__name__}")
    result = float(value)
    if positive and result <= 0:
        raise ConfigError(f"{path} must be > 0, got {value}")
    return result


def _as_str(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{path} must not be empty")
    return value


def _as_int_list(
    value: Any, path: str, *, minimum: int = 0, allow_empty: bool = False
) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be an array, got {type(value).__name__}")
    if not value and not allow_empty:
        raise ConfigError(f"{path} must not be empty")
    result: list[int] = []
    for index, item in enumerate(value):
        result.append(_as_int(item, f"{path}[{index}]", minimum=minimum))
    if len(set(result)) != len(result):
        raise ConfigError(f"{path} must not contain duplicates")
    return tuple(result)


def _resolve_path(base: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return str(path)


def _parse_mysql(data: Any, base: Path) -> MySqlConfig:
    raw = _mapping(data, "mysql")
    _reject_unknown(raw, {"host", "port", "user", "password", "database", "charset", "connect_timeout"}, "mysql")
    host = _as_str(_get(raw, "host", "mysql"), "mysql.host")
    user = _as_str(_get(raw, "user", "mysql"), "mysql.user")
    password = _as_str(_get(raw, "password", "mysql", ""), "mysql.password", allow_empty=True)
    database = _as_str(_get(raw, "database", "mysql"), "mysql.database")
    return MySqlConfig(
        host=host,
        port=_as_int(_get(raw, "port", "mysql", 3306), "mysql.port", minimum=1, maximum=65535),
        user=user,
        password=password,
        database=database,
        charset=_as_str(_get(raw, "charset", "mysql", "utf8"), "mysql.charset"),
        connect_timeout=_as_int(
            _get(raw, "connect_timeout", "mysql", 10), "mysql.connect_timeout", minimum=1
        ),
    )


def _parse_mes4(data: Any) -> Mes4Config:
    raw = _mapping(data, "mes4")
    _reject_unknown(raw, {"host", "port", "timeout_s"}, "mes4")
    return Mes4Config(
        host=_as_str(_get(raw, "host", "mes4"), "mes4.host"),
        port=_as_int(_get(raw, "port", "mes4", 2000), "mes4.port", minimum=1, maximum=65535),
        timeout_s=_as_float(
            _get(raw, "timeout_s", "mes4", 5), "mes4.timeout_s", positive=True
        ),
    )


def _parse_fleet(data: Any) -> FleetManagerConfig:
    raw = _mapping(data, "fleetManager")
    _reject_unknown(raw, {"host", "timeout_s"}, "fleetManager")
    host = _as_str(_get(raw, "host", "fleetManager"), "fleetManager.host")
    if not host.startswith(("http://", "https://")):
        raise ConfigError("fleetManager.host must start with http:// or https://")
    return FleetManagerConfig(
        host=host,
        timeout_s=_as_float(
            _get(raw, "timeout_s", "fleetManager", 5), "fleetManager.timeout_s", positive=True
        ),
    )


def _parse_planner(data: Any, base: Path) -> PlannerSettings:
    raw = _mapping(data, "planner") if data is not None else {}
    _reject_unknown(
        raw,
        {"cycle_s", "buffer_cycle_s", "lock_file", "pause_file", "log_file", "log_max_bytes", "log_backups"},
        "planner",
    )
    return PlannerSettings(
        cycle_s=_as_float(_get(raw, "cycle_s", "planner", 5), "planner.cycle_s", positive=True),
        buffer_cycle_s=_as_float(
            _get(raw, "buffer_cycle_s", "planner", 5), "planner.buffer_cycle_s", positive=True
        ),
        lock_file=_resolve_path(
            base, _as_str(_get(raw, "lock_file", "planner", ".planner.lock"), "planner.lock_file")
        ),
        pause_file=_resolve_path(
            base, _as_str(_get(raw, "pause_file", "planner", ".planner.pause"), "planner.pause_file")
        ),
        log_file=_resolve_path(
            base, _as_str(_get(raw, "log_file", "planner", "planner.log"), "planner.log_file")
        ),
        log_max_bytes=_as_int(
            _get(raw, "log_max_bytes", "planner", 1_048_576), "planner.log_max_bytes", minimum=0
        ),
        log_backups=_as_int(_get(raw, "log_backups", "planner", 3), "planner.log_backups", minimum=0),
    )


def _parse_box(data: Any) -> BoxConfig:
    raw = _mapping(data, "box") if data is not None else {}
    _reject_unknown(raw, {"type_pno", "capacity"}, "box")
    return BoxConfig(
        type_pno=_as_int(_get(raw, "type_pno", "box", 29), "box.type_pno", minimum=1),
        capacity=_as_int(_get(raw, "capacity", "box", 8), "box.capacity", minimum=1, maximum=1000),
    )


def _parse_pick_and_sort(data: Any) -> PickAndSortConfig:
    raw = _mapping(data, "pickAndSort") if data is not None else {}
    _reject_unknown(
        raw,
        {"numberOfBuffers", "numberOfFreeBuffers", "bufferPosPriorities", "disabledBuffers"},
        "pickAndSort",
    )
    number_of_buffers = _as_int(
        _get(raw, "numberOfBuffers", "pickAndSort", 6),
        "pickAndSort.numberOfBuffers",
        minimum=1,
    )
    number_of_free = _as_int(
        _get(raw, "numberOfFreeBuffers", "pickAndSort", 2),
        "pickAndSort.numberOfFreeBuffers",
        minimum=0,
    )
    if number_of_free > number_of_buffers:
        raise ConfigError(
            "pickAndSort.numberOfFreeBuffers must not exceed pickAndSort.numberOfBuffers"
        )
    priorities = _as_int_list(
        _get(raw, "bufferPosPriorities", "pickAndSort", [5, 4, 1, 3]),
        "pickAndSort.bufferPosPriorities",
        minimum=1,
        allow_empty=True,
    )
    disabled = _as_int_list(
        _get(raw, "disabledBuffers", "pickAndSort", [2, 6]),
        "pickAndSort.disabledBuffers",
        minimum=1,
        allow_empty=True,
    )
    for position in (*priorities, *disabled):
        if position > number_of_buffers:
            raise ConfigError(
                f"pickAndSort buffer position {position} is outside 1..{number_of_buffers}"
            )
    usable_buffers = number_of_buffers - len(disabled)
    if number_of_free > usable_buffers:
        raise ConfigError(
            f"pickAndSort.numberOfFreeBuffers ({number_of_free}) must not exceed the "
            f"{usable_buffers} buffer(s) that are not disabled"
        )
    for position in priorities:
        if position in disabled:
            log.warning(
                "pickAndSort.bufferPosPriorities contains disabled buffer %s; "
                "it will never be released",
                position,
            )
    return PickAndSortConfig(
        number_of_buffers=number_of_buffers,
        number_of_free_buffers=number_of_free,
        buffer_pos_priorities=priorities,
        disabled_buffers=disabled,
    )


def _parse_resource(value: Any, index: int) -> ResourceConfig:
    path = f"resources[{index}]"
    raw = _mapping(value, path)
    _reject_unknown(
        raw,
        {
            "name",
            "resourceId",
            "workplanNumber",
            "workplanDisassemblyNumber",
            "feedOperationNumbers",
            "removeOperationNumbers",
            "directDelivery",
        },
        path,
    )
    workplan_number = _as_int(
        _get(raw, "workplanNumber", path), f"{path}.workplanNumber", minimum=0
    )
    workplan_disassembly_number = _as_int(
        _get(raw, "workplanDisassemblyNumber", path),
        f"{path}.workplanDisassemblyNumber",
        minimum=0,
    )
    direct_delivery = _as_str(
        _get(raw, "directDelivery", path, "off"), f"{path}.directDelivery"
    )
    if direct_delivery not in _DIRECT_DELIVERY_MODES:
        raise ConfigError(
            f"{path}.directDelivery must be one of off, prefer, only, "
            f"got {direct_delivery!r}"
        )
    if direct_delivery != "off" and (
        workplan_number <= 0 or workplan_disassembly_number <= 0
    ):
        raise ConfigError(
            f"{path}.directDelivery = {direct_delivery!r} requires workplanNumber > 0 "
            "and workplanDisassemblyNumber > 0 (this resource has no store workplan)"
        )
    return ResourceConfig(
        name=_as_str(_get(raw, "name", path), f"{path}.name"),
        resource_id=_as_int(_get(raw, "resourceId", path), f"{path}.resourceId", minimum=1),
        workplan_number=workplan_number,
        workplan_disassembly_number=workplan_disassembly_number,
        feed_operation_numbers=_as_int_list(
            _get(raw, "feedOperationNumbers", path), f"{path}.feedOperationNumbers", minimum=0
        ),
        remove_operation_numbers=_as_int_list(
            _get(raw, "removeOperationNumbers", path), f"{path}.removeOperationNumbers", minimum=0
        ),
        direct_delivery=direct_delivery,
    )


def parse_config(data: Any, base_dir: str | Path = ".") -> Config:
    """Validate a decoded JSON object and return a :class:`Config`."""
    raw = _mapping(data, "config")
    _reject_unknown(
        raw,
        {
            "mysql",
            "mes4",
            "fleetManager",
            "planner",
            "box",
            "asrsResourceId",
            "pickAndSortResourceId",
            "pickAndSort",
            "resources",
        },
        "config",
    )
    base = Path(base_dir)

    resource_values = _get(raw, "resources", "config")
    if not isinstance(resource_values, list) or not resource_values:
        raise ConfigError("config.resources must be a non-empty array")
    resources = tuple(_parse_resource(value, index) for index, value in enumerate(resource_values))

    names = [resource.name for resource in resources]
    if len(set(names)) != len(names):
        raise ConfigError("config.resources contains duplicate names")
    ids = [resource.resource_id for resource in resources]
    if len(set(ids)) != len(ids):
        raise ConfigError("config.resources contains duplicate resourceId values")

    asrs_id = _as_int(
        _get(raw, "asrsResourceId", "config", 17), "config.asrsResourceId", minimum=1
    )
    pick_id = _as_int(
        _get(raw, "pickAndSortResourceId", "config", 19),
        "config.pickAndSortResourceId",
        minimum=1,
    )
    if asrs_id == pick_id:
        raise ConfigError("asrsResourceId and pickAndSortResourceId must differ")

    return Config(
        mysql=_parse_mysql(_get(raw, "mysql", "config"), base),
        mes4=_parse_mes4(_get(raw, "mes4", "config")),
        fleet_manager=_parse_fleet(_get(raw, "fleetManager", "config")),
        planner=_parse_planner(raw.get("planner"), base),
        box=_parse_box(raw.get("box")),
        asrs_resource_id=asrs_id,
        pick_and_sort_resource_id=pick_id,
        pick_and_sort=_parse_pick_and_sort(raw.get("pickAndSort")),
        resources=resources,
    )


def load_config(path: str | Path) -> Config:
    """Load and validate ``config.json``.

    Relative ``planner.lock_file``/``pause_file``/``log_file`` paths are
    resolved against the directory containing the config file.
    """
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    try:
        return parse_config(data, config_path.resolve().parent)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
