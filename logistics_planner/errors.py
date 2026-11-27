"""Exception hierarchy for the logistics planner.

Every failure is raised as a :class:`PlannerError` (or a subclass) and aborts
the current planning/booking cycle.  There are deliberately no silent
fallbacks to ``0`` or ``False``.
"""

from __future__ import annotations


class PlannerError(Exception):
    """Base class for every planner failure."""


class ConfigError(PlannerError):
    """The configuration file is missing, malformed or inconsistent."""


class PlanningError(PlannerError):
    """The database contains an ambiguous or impossible planning state."""


class CapacityError(PlanningError):
    """A target box has no (or too few) free slots for the requested parts."""


class DatabaseError(PlannerError):
    """A database operation failed."""


class QueryRowcountError(DatabaseError):
    """A guarded UPDATE did not affect exactly one row."""


class ConcurrentWriteError(QueryRowcountError):
    """Another writer changed the row between SELECT ... FOR UPDATE and UPDATE."""


class MesTcpError(PlannerError):
    """The MES4 TCP connection failed, timed out or returned garbage."""


class MesCommandError(MesTcpError):
    """MES4 accepted the connection but rejected the command."""


class FleetManagerError(PlannerError):
    """The Fleet Manager HTTP endpoint is unreachable or returned bad data."""


class LockFileError(PlannerError):
    """Another planner instance already holds the single-instance lock."""
