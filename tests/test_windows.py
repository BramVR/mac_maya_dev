from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import TextIO

import pytest

from mac_maya_dev.config import Config
from mac_maya_dev.errors import MayaDevError
from mac_maya_dev.remote import Result, Runner
from mac_maya_dev.windows import (
    _asset_text,
    _locked_packages,
    task_command,
    windows_check,
    windows_setup,
)


class FakeRunner(Runner):
    def __init__(self, results: list[Result]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.stdin_scripts: list[str] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        capture: bool = True,
        stdin: TextIO | None = None,
    ) -> Result:
        del cwd, capture
        self.calls.append(list(args))
        if stdin is not None:
            self.stdin_scripts.append(stdin.read())
        if not self.results:
            raise AssertionError(f"Unexpected command: {args}")
        return self.results.pop(0)


def decode_ssh_script(call: list[str]) -> str:
    encoded = call[call.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def check_payload(*failed: str) -> dict[str, object]:
    ids = [
        "host.console_user",
        "host.git",
        "maya.executable",
        "python.base",
        "mcp.runtime",
        "mcp.lock",
        "mcp.dependencies",
        "mcp.source_import",
        "sessiond.runtime",
        "sessiond.reproducibility",
        "directory.setup_root",
        "directory.deploy_root",
        "launcher.content",
        "task.interactive",
        "port.command",
    ]
    failed_ids = set(failed)
    checks = [
        {
            "id": check_id,
            "ok": check_id not in failed_ids,
            "required": check_id not in {"host.git", "sessiond.reproducibility"},
            "status": "warn"
            if check_id == "sessiond.reproducibility"
            else ("fail" if check_id in failed_ids else "pass"),
            "remediation": f"fix {check_id}",
            "actual": (
                {"directory_exists": False, "version": None}
                if check_id == "python.base" and check_id in failed_ids
                else {"exists": False, "kind": "absent"}
                if check_id.startswith("directory.") and check_id in failed_ids
                else {"exists": False, "kind": "absent"}
                if check_id == "launcher.content" and check_id in failed_ids
                else None
            ),
        }
        for check_id in ids
    ]
    return {
        "schema_version": 1,
        "command": "windows.check",
        "mode": "check",
        "ok": not failed_ids,
        "changed": False,
        "checks": checks,
    }


def result(payload: dict[str, object], *, code: int = 0) -> Result:
    return Result(code, json.dumps(payload), "")


def test_windows_check_uses_one_read_only_encoded_stdin_call(config: Config) -> None:
    payload = check_payload("python.base")
    runner = FakeRunner([result(payload)])

    assert windows_check(config, runner) == payload
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == "ssh"
    bootstrap = decode_ssh_script(runner.calls[0])
    assert "[Console]::In.ReadToEnd()" in bootstrap
    stdin_payload = json.loads(runner.stdin_scripts[0])
    script = stdin_payload["script"]
    assert "Get-ScheduledTask" in script
    assert "Get-NetTCPConnection" in script
    assert "New-Item" not in script
    assert "Register-ScheduledTask" not in script
    assert "Invoke-WebRequest" not in script
    assert "trigger_count" in script
    assert 'RunLevel -eq "Limited"' in script
    assert "WorkingDirectory" in script
    assert "Settings.Enabled" in script
    assert "MultipleInstances" in script
    assert "ExecutionTimeLimit" in script
    assert "importlib.metadata" in script
    assert ".maya-dev-requirements.lock" in script
    assert "Remove-Item Env:PYTHONHOME, Env:PYTHONPATH" in script
    assert "-I -B" in script


def test_windows_setup_defaults_to_write_free_plan(config: Config) -> None:
    check = check_payload(
        "python.base",
        "mcp.runtime",
        "mcp.lock",
        "mcp.dependencies",
        "directory.setup_root",
        "launcher.content",
        "task.interactive",
    )
    runner = FakeRunner([result(check)])

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["changed"] is False
    assert [item["id"] for item in payload["actions"]] == [
        "directories",
        "python.base",
        "mcp.runtime",
        "launcher.content",
        "task.interactive",
    ]
    assert len(runner.calls) == 1


def test_windows_setup_reports_blocker_without_writes(config: Config) -> None:
    runner = FakeRunner([result(check_payload("port.command", "python.base"))])

    payload = windows_setup(config, runner, apply=True)

    assert payload["ok"] is False
    assert payload["mode"] == "apply"
    assert payload["blockers"] == [
        {"id": "port.command", "remediation": "fix port.command"}
    ]
    assert len(runner.calls) == 1


def test_windows_setup_does_not_block_on_informational_git_check(config: Config) -> None:
    runner = FakeRunner([result(check_payload("host.git", "python.base"))])

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is True
    assert payload["blockers"] == []
    assert [item["id"] for item in payload["actions"]] == ["python.base"]


def test_windows_setup_blocks_occupied_incompatible_python_target(config: Config) -> None:
    check = check_payload("python.base")
    checks = check["checks"]
    assert isinstance(checks, list)
    python_check = next(item for item in checks if item["id"] == "python.base")
    python_check["actual"] = {"directory_exists": True, "version": "3.11.8"}
    runner = FakeRunner([result(check)])

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is False
    assert payload["actions"] == []
    assert payload["blockers"] == [
        {"id": "python.base", "remediation": "fix python.base"}
    ]


def test_windows_setup_allows_rebuild_to_repair_source_import(config: Config) -> None:
    runner = FakeRunner(
        [result(check_payload("mcp.dependencies", "mcp.source_import"))]
    )

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is True
    assert payload["blockers"] == []
    assert [item["id"] for item in payload["actions"]] == ["mcp.runtime"]


def test_windows_setup_blocks_required_directory_occupied_by_file(
    config: Config,
) -> None:
    check = check_payload("directory.setup_root")
    checks = check["checks"]
    assert isinstance(checks, list)
    directory_check = next(
        item for item in checks if item["id"] == "directory.setup_root"
    )
    directory_check["actual"] = {"exists": True, "kind": "other"}
    runner = FakeRunner([result(check)])

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is False
    assert payload["actions"] == []
    assert payload["blockers"] == [
        {"id": "directory.setup_root", "remediation": "fix directory.setup_root"}
    ]


def test_windows_setup_blocks_launcher_path_occupied_by_directory(
    config: Config,
) -> None:
    check = check_payload("launcher.content")
    checks = check["checks"]
    assert isinstance(checks, list)
    launcher_check = next(item for item in checks if item["id"] == "launcher.content")
    launcher_check["actual"] = {"exists": True, "kind": "other"}
    runner = FakeRunner([result(check)])

    payload = windows_setup(config, runner, apply=False)

    assert payload["ok"] is False
    assert payload["actions"] == []
    assert payload["blockers"] == [
        {"id": "launcher.content", "remediation": "fix launcher.content"}
    ]


def test_windows_setup_second_apply_is_noop(config: Config) -> None:
    runner = FakeRunner([result(check_payload())])

    payload = windows_setup(config, runner, apply=True)

    assert payload["ok"] is True
    assert payload["changed"] is False
    assert payload["changes"] == []
    assert len(runner.calls) == 1


def test_windows_setup_apply_uploads_then_verifies(config: Config) -> None:
    before = check_payload("python.base", "mcp.runtime", "launcher.content", "task.interactive")
    setup = {
        "schema_version": 1,
        "command": "windows.setup",
        "mode": "apply",
        "ok": True,
        "changed": True,
        "changes": [{"id": "python.base", "status": "changed"}],
    }
    after = check_payload()
    runner = FakeRunner(
        [
            result(before),
            result({"ok": True, "stage": "C:/Temp/stage", "bundle": "C:/Temp/stage/setup.zip"}),
            Result(0, "", ""),
            result(setup),
            Result(0, "", ""),
            result(after),
        ]
    )

    payload = windows_setup(config, runner, apply=True)

    assert payload["ok"] is True
    assert payload["post_check"] == after
    assert [call[0] for call in runner.calls] == [
        "ssh",
        "ssh",
        "scp",
        "ssh",
        "ssh",
        "ssh",
    ]
    apply_script = decode_ssh_script(runner.calls[3])
    assert "Expand-Archive" in apply_script
    assert "Remove-Item -LiteralPath $stage" in apply_script
    assert len(runner.calls[3][runner.calls[3].index("-EncodedCommand") + 1]) < 8000


def test_setup_builds_windows_venv_at_final_path_with_backup_rollback() -> None:
    script = _asset_text("setup-maya2024.ps1")

    assert "-m venv $McpVenvDir" in script
    assert ".staging-" not in script
    assert "[System.IO.Directory]::Move($McpVenvDir, $backupVenv)" in script
    assert "[System.IO.Directory]::Move($backupVenv, $McpVenvDir)" in script
    assert "Settings.Enabled" in script
    assert "MultipleInstances" in script
    assert "ExecutionTimeLimit" in script
    assert "Remove-Item Env:PYTHONHOME, Env:PYTHONPATH" in script
    assert "-I -B" in script
    assert "Global\\mac_maya_dev_windows_setup" in script
    assert 'Global\\mac_maya_dev_command_port_$Port' in script
    assert "$setupMutex.ReleaseMutex()" in script
    assert "$modeMutex.ReleaseMutex()" in script


def test_launcher_uses_same_isolated_python_environment_as_validation() -> None:
    script = _asset_text("start-sessiond-maya2024.ps1")

    assert "Remove-Item Env:PYTHONHOME, Env:PYTHONPATH" in script
    assert "$SessiondPython -I -B -m $SessiondModule" in script


def test_windows_setup_preserves_structured_partial_failure(config: Config) -> None:
    before = check_payload("launcher.content")
    failure = {
        "schema_version": 1,
        "command": "windows.setup",
        "mode": "apply",
        "ok": False,
        "changed": True,
        "changes": [{"id": "directory", "status": "changed"}],
        "error": "task registration denied",
    }
    runner = FakeRunner(
        [
            result(before),
            result({"ok": True, "stage": "C:/Temp/stage", "bundle": "C:/Temp/stage/setup.zip"}),
            Result(0, "", ""),
            result(failure, code=1),
            Result(0, "", ""),
        ]
    )

    payload = windows_setup(config, runner, apply=True)

    assert payload["ok"] is False
    assert payload["changed"] is True
    assert payload["error"] == "task registration denied"
    assert len(runner.calls) == 5


def test_windows_setup_preserves_changes_when_post_check_transport_fails(
    config: Config,
) -> None:
    before = check_payload("launcher.content")
    setup = {
        "schema_version": 1,
        "command": "windows.setup",
        "mode": "apply",
        "ok": True,
        "changed": True,
        "changes": [{"id": "launcher.content", "status": "changed"}],
    }
    runner = FakeRunner(
        [
            result(before),
            result(
                {
                    "ok": True,
                    "stage": "C:/Temp/stage",
                    "bundle": "C:/Temp/stage/setup.zip",
                }
            ),
            Result(0, "", ""),
            result(setup),
            Result(0, "", ""),
            Result(255, "", "connection lost"),
        ]
    )

    payload = windows_setup(config, runner, apply=True)

    assert payload["ok"] is False
    assert payload["changed"] is True
    assert payload["changes"] == setup["changes"]
    assert payload["verification"] == {
        "ok": False,
        "error": "Windows host check failed: connection lost",
    }


def test_windows_setup_transport_failure_still_attempts_remote_cleanup(
    config: Config,
) -> None:
    before = check_payload("launcher.content")
    runner = FakeRunner(
        [
            result(before),
            result(
                {
                    "ok": True,
                    "stage": "C:/Temp/stage",
                    "bundle": "C:/Temp/stage/setup.zip",
                }
            ),
            Result(0, "", ""),
            Result(255, "", "connection lost"),
            Result(0, "", ""),
        ]
    )

    with pytest.raises(MayaDevError, match="connection lost"):
        windows_setup(config, runner, apply=True)

    assert len(runner.calls) == 5
    assert "Remove-Item" in decode_ssh_script(runner.calls[-1])


def test_windows_setup_upload_failure_reports_cleanup_failure(config: Config) -> None:
    before = check_payload("launcher.content")
    runner = FakeRunner(
        [
            result(before),
            result({"ok": True, "stage": "C:/Temp/stage", "bundle": "C:/Temp/stage/setup.zip"}),
            Result(1, "", "connection lost"),
            Result(1, "", "cleanup denied"),
        ]
    )

    with pytest.raises(
        MayaDevError, match="connection lost; remote cleanup also failed: cleanup denied"
    ):
        windows_setup(config, runner, apply=True)


def test_task_command_encodes_launcher_values_as_powershell_literals(config: Config) -> None:
    executable, arguments = task_command(config)
    encoded = arguments.rsplit(" ", 1)[-1]
    script = base64.b64decode(encoded).decode("utf-16-le")

    assert executable == "powershell.exe"
    assert script.startswith("& 'C:/maya-dev/start-sessiond-maya2024.ps1'")
    assert "-ExecutionPolicy Bypass" in arguments
    assert "-MayaExe 'C:/Program Files/Autodesk/Maya2024/bin/maya.exe'" in script
    assert "-SessiondModule 'gg_maya_sessiond.cli'" in script
    assert script.endswith("exit $LASTEXITCODE\n")


def test_task_command_keeps_apostrophe_in_path_as_data(config: Config) -> None:
    assert config.windows is not None
    changed = replace(
        config,
        windows=replace(config.windows, launcher="C:/maya-dev/Bram's/start-sessiond.ps1"),
    )

    _, arguments = task_command(changed)
    encoded = arguments.rsplit(" ", 1)[-1]
    script = base64.b64decode(encoded).decode("utf-16-le")

    assert "& 'C:/maya-dev/Bram''s/start-sessiond.ps1'" in script


def test_mcp_lock_has_only_exact_normalized_package_pins() -> None:
    packages = _locked_packages()

    assert packages["fastmcp"]
    assert packages["typing-extensions"]
    assert all(name == name.casefold() and "_" not in name for name in packages)
    assert all(
        version and not any(char.isspace() for char in version)
        for version in packages.values()
    )
