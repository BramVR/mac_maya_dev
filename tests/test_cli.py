from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import mac_maya_dev.cli as cli
from mac_maya_dev.cli import _run, build_parser, main
from mac_maya_dev.remote import Runner


def test_parser_accepts_call_inputs() -> None:
    args = build_parser().parse_args(["call", "nodes.list", "type=transform"])
    assert args.command == "call"
    assert args.tool == "nodes.list"
    assert args.pairs == ["type=transform"]


def test_main_reports_missing_config(tmp_path: Path, capsys: object) -> None:
    code = main(["--config", str(tmp_path / "missing.toml"), "doctor"])
    assert code == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "Config not found" in captured.err


@pytest.mark.parametrize(
    ("command", "operation", "operation_result", "expected_code"),
    [
        ("doctor", "doctor", {"ok": False}, 1),
        ("check", "check_source", {"ok": True}, 0),
        ("deploy", "deploy", {"ok": True, "content_hash": "abc"}, 0),
        ("connect", "connect", 7, 7),
        (
            "status",
            "session_status",
            {"state": {"status": "running"}, "derived_status": "running"},
            0,
        ),
        ("start", "session_start", {"ok": True}, 0),
        ("stop", "session_stop", {"ok": True}, 0),
        ("restart", "session_restart", {"ok": True}, 0),
        ("call scene.info", "session_call", {"ok": False}, 1),
    ],
)
def test_run_dispatches_commands(
    monkeypatch: pytest.MonkeyPatch,
    config: object,
    command: str,
    operation: str,
    operation_result: Any,
    expected_code: int,
) -> None:
    monkeypatch.setattr(cli, "find_config", lambda _path: Path("config.toml"))
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, operation, lambda *_args, **_kwargs: operation_result)
    args = build_parser().parse_args(command.split())
    code, payload = _run(args, Runner())
    assert code == expected_code
    if operation != "connect":
        assert payload == operation_result


def test_run_status_is_nonzero_when_session_is_inactive(
    monkeypatch: pytest.MonkeyPatch, config: object
) -> None:
    payload = {"state": {"status": "stopped"}, "derived_status": "stopped"}
    monkeypatch.setattr(cli, "find_config", lambda _path: Path("config.toml"))
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "session_status", lambda *_args: payload)
    code, returned = _run(build_parser().parse_args(["status"]), Runner())
    assert code == 1
    assert returned == payload
