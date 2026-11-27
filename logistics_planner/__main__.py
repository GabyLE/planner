"""Command line entry point: ``python -m logistics_planner run|plan|block``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config, load_config
from .db import Db
from .decisions import JsonlDecisionLog
from .errors import ConfigError, PlannerError
from .fleet import FleetManager
from .loop import InstanceLock, PlannerRunner
from .mes_tcp import MesTcp
from .queries import Repository

log = logging.getLogger("logistics_planner")

_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _add_flags(parser: argparse.ArgumentParser, *, suppressed: bool) -> None:
    """Add planner flags.

    They are available before the subcommand (integration/harness style) and
    after it (``planner run --once``).  The subparser copies use
    ``argparse.SUPPRESS`` so an absent after-command flag does not overwrite a
    value that was already parsed before the command.
    """
    default = argparse.SUPPRESS if suppressed else None
    parser.add_argument(
        "--config",
        default=default if suppressed else str(_DEFAULT_CONFIG),
        help=f"path to config.json (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--log-level",
        default=default if suppressed else "INFO",
        choices=_LOG_LEVELS,
        type=str.upper,
        help="console/file log level (default: INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="read-only shadow mode: log intended writes/TCP, never change the DB",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="copy-DB simulation: insert fake MES rows and roll the whole cycle back",
    )
    parser.add_argument(
        "--i-know-this-is-a-copy",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="allow --simulate against a database named mes4",
    )
    parser.add_argument(
        "--record-decisions",
        metavar="PATH",
        default=default,
        help="append every source-slot decision (context, candidates, choice) to PATH as JSONL",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="run one block + plan cycle and exit (run command)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logistics_planner",
        description="Festo logistics planner: commissioning and disassembly orders",
    )
    _add_flags(parser, suppressed=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    helps = {
        "run": "run the planning + buffer-blocking loop",
        "plan": "run one planning cycle (commissioning, booking, disassembly)",
        "block": "run one Pick & Sort buffer-blocking cycle",
    }
    for name, help_text in helps.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        _add_flags(sub, suppressed=True)
    return parser


def _setup_logging(config: Config, level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logger = logging.getLogger("logistics_planner")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    logger.addHandler(console)

    log_path = Path(config.planner.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.planner.log_max_bytes,
        backupCount=config.planner.log_backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"planner: {exc}", file=sys.stderr)
        return 2

    if args.dry_run and args.simulate:
        print("planner: --dry-run and --simulate are mutually exclusive", file=sys.stderr)
        return 2
    if args.simulate and not args.i_know_this_is_a_copy:
        if config.mysql.database.strip().lower() == "mes4":
            print(
                "planner: refusing --simulate against the live 'mes4' database; "
                "use --i-know-this-is-a-copy for a copy",
                file=sys.stderr,
            )
            return 2

    try:
        _setup_logging(config, args.log_level)
    except (OSError, PlannerError) as exc:
        print(f"planner: cannot set up logging: {exc}", file=sys.stderr)
        return 2

    db: Db | None = None
    try:
        db = Db(config, dry_run=args.dry_run, simulate=args.simulate)
        db.connect()
        repository = Repository(db, config)
        mes = MesTcp(
            config.mes4,
            read_only=args.dry_run,
            simulate=args.simulate,
            repository=repository,
        )
        fleet = FleetManager(config.fleet_manager)
        decisions = None
        if args.record_decisions:
            mode = "dry-run" if args.dry_run else "simulate" if args.simulate else "live"
            decisions = JsonlDecisionLog(args.record_decisions, mode=mode)
            log.info("Recording decisions to %s", args.record_decisions)
        runner = PlannerRunner(config, repository, mes, fleet, decisions=decisions)

        if args.dry_run:
            log.info(
                "[dry-run] read-only shadow mode: intended writes and MES commands "
                "are logged; the database is not changed"
            )
        elif args.simulate:
            log.info(
                "[simulate] copy-DB mode: simulated MES rows and planner writes "
                "roll back at the end of each cycle"
            )

        if args.command == "run":
            if args.once:
                runner.run_once()
            else:
                runner.run_forever()
        elif args.command == "plan":
            # A manual cycle must not race a running planner either.
            with InstanceLock(config.planner.lock_file):
                runner.run_plan_cycle()
        elif args.command == "block":
            with InstanceLock(config.planner.lock_file):
                runner.run_block_cycle()
        else:  # pragma: no cover - argparse enforces the choices
            raise ConfigError(f"unknown command {args.command!r}")
        return 0
    except PlannerError as exc:
        log.error("planner failed: %s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        log.info("Interrupted")
        return 0
    except Exception:
        log.exception("planner failed unexpectedly")
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
