"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import find_config, load_config
from .errors import MayaDevError
from .operations import (
    check_source,
    connect,
    deploy,
    doctor,
    session_call,
    session_restart,
    session_start,
    session_status,
    session_stop,
)
from .remote import Runner, emit
from .windows import windows_check, windows_setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maya-dev",
        description="Develop GG_MayaMCP on a Mac and run it beside Maya on Windows.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="TOML config path")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Read-only local and Windows checks")
    sub.add_parser("check", help="Run configured checks in the local source checkout")
    sub.add_parser("deploy", help="Upload a clean content-addressed source snapshot")
    sub.add_parser("connect", aliases=["mcp"], help="Run remote MCP over SSH stdio")
    sub.add_parser("status", help="Read sessiond status")
    sub.add_parser("start", help="Cold-start Maya and MCP through sessiond")
    sub.add_parser("stop", help="Stop the sessiond-managed Maya session")
    sub.add_parser("restart", help="Cold-restart the sessiond-managed Maya session")

    windows = sub.add_parser("windows", help="Inspect or prepare the configured Windows host")
    windows_sub = windows.add_subparsers(dest="windows_command", required=True)
    windows_sub.add_parser("check", help="Read-only Windows prerequisite and configuration report")
    windows_setup_parser = windows_sub.add_parser(
        "setup", help="Preview or apply idempotent Windows host preparation"
    )
    windows_setup_parser.add_argument(
        "--apply", action="store_true", help="Apply the plan; default is read-only dry-run"
    )

    call = sub.add_parser("call", help="Call a tool through the sessiond worker")
    call.add_argument("tool", nargs="?")
    call.add_argument("pairs", nargs="*", help="Tool input as key=value")
    call.add_argument("--input-json")
    call.add_argument("--list", action="store_true", dest="list_tools")
    call.add_argument("--tool-help", action="store_true")
    return parser


def _run(args: argparse.Namespace, runner: Runner) -> tuple[int, Any | None]:
    config = load_config(find_config(args.config))
    if args.command == "doctor":
        payload = doctor(config, runner)
        return (0 if payload["ok"] else 1), payload
    if args.command == "check":
        payload = check_source(config, runner)
        return (0 if payload["ok"] else 1), payload
    if args.command == "deploy":
        return 0, deploy(config, runner)
    if args.command in {"connect", "mcp"}:
        return connect(config, runner), None
    if args.command == "status":
        payload = session_status(config, runner)
        active = payload.get("derived_status") in {"running", "starting", "stopping"}
        return (0 if active else 1), payload
    if args.command == "start":
        payload = session_start(config, runner)
        return (0 if payload.get("ok") else 1), payload
    if args.command == "stop":
        payload = session_stop(config, runner)
        return (0 if payload.get("ok") else 1), payload
    if args.command == "restart":
        payload = session_restart(config, runner)
        return (0 if payload.get("ok") else 1), payload
    if args.command == "windows" and args.windows_command == "check":
        payload = windows_check(config, runner)
        return (0 if payload.get("ok") else 1), payload
    if args.command == "windows" and args.windows_command == "setup":
        payload = windows_setup(config, runner, apply=args.apply)
        return (0 if payload.get("ok") else 1), payload
    if args.command == "call":
        payload = session_call(
            config,
            runner,
            tool=args.tool,
            pairs=args.pairs,
            input_json=args.input_json,
            list_tools=args.list_tools,
            tool_help=args.tool_help,
        )
        return (0 if payload.get("ok", True) else 1), payload
    raise MayaDevError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload = _run(args, Runner())
        if payload is not None:
            emit(payload, json_output=args.json)
        return code
    except MayaDevError as exc:
        if args.json:
            emit({"ok": False, "error": str(exc)}, json_output=True)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
