from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import TextIO

import pytest

from mac_maya_dev.config import Config, RemoteConfig
from mac_maya_dev.errors import MayaDevError
from mac_maya_dev.operations import (
    check_source,
    connect,
    deploy,
    doctor,
    session_call,
    session_restart,
    session_start,
    session_status,
)
from mac_maya_dev.remote import Result, Runner


class FakeRunner(Runner):
    def __init__(self, results: list[Result], *, exec_code: int = 0) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.exec_calls: list[list[str]] = []
        self.exec_code = exec_code

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        capture: bool = True,
        stdin: TextIO | None = None,
    ) -> Result:
        del cwd, capture, stdin
        self.calls.append(list(args))
        if not self.results:
            raise AssertionError(f"Unexpected command: {args}")
        return self.results.pop(0)

    def exec(self, args: Sequence[str]) -> int:
        self.exec_calls.append(list(args))
        return self.exec_code


def decode_ssh_script(call: list[str]) -> str:
    encoded = call[call.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_check_source_stops_after_first_failure(config: Config) -> None:
    runner = FakeRunner([Result(0, "lint ok", ""), Result(1, "", "tests failed")])
    payload = check_source(config, runner)
    assert payload["ok"] is False
    assert len(payload["results"]) == 2
    assert payload["results"][1]["stderr"] == "tests failed"


def test_doctor_combines_local_and_remote_checks(config: Config) -> None:
    remote = {
        "ok": True,
        "python_exists": True,
        "python_runnable": True,
        "python_version": "Python 3.11.9",
        "maya_exists": True,
        "sessiond_exists": True,
        "sessiond_importable": True,
        "sessiond_version": "gg-maya-session 0.1.0",
        "interactive_task_exists": True,
        "interactive_task_logon_type": "Interactive",
        "interactive_task_state": "Ready",
        "deploy_root_exists": True,
        "command_port": 7001,
        "command_port_listening": True,
        "command_port_loopback_only": True,
        "command_port_addresses": ["127.0.0.1"],
        "command_port_processes": ["maya"],
        "command_port_process_paths": [config.sessiond.maya_exe],
    }
    runner = FakeRunner(
        [Result(0, "host maya-win", ""), Result(0, __import__("json").dumps(remote), "")]
    )
    payload = doctor(config, runner)
    assert payload["ok"] is True
    assert payload["remote"]["command_port_processes"] == ["maya"]


def test_doctor_rejects_command_port_owned_by_wrong_maya(config: Config) -> None:
    remote = {
        "ok": True,
        "python_exists": True,
        "python_runnable": True,
        "maya_exists": True,
        "sessiond_importable": True,
        "interactive_task_exists": True,
        "interactive_task_logon_type": "Interactive",
        "command_port_listening": True,
        "command_port_loopback_only": True,
        "command_port_addresses": ["127.0.0.1"],
        "command_port_processes": ["maya"],
        "command_port_process_paths": ["C:/Program Files/Autodesk/Maya2025/bin/maya.exe"],
    }
    runner = FakeRunner(
        [Result(0, "host maya-win", ""), Result(0, __import__("json").dumps(remote), "")]
    )
    payload = doctor(config, runner)
    command_port = next(
        check for check in payload["checks"] if check["name"] == "remote.command_port"
    )
    assert command_port["ok"] is False
    assert payload["ok"] is False


def test_doctor_rejects_non_loopback_command_port(config: Config) -> None:
    remote = {
        "ok": True,
        "python_exists": True,
        "python_runnable": True,
        "maya_exists": True,
        "sessiond_importable": True,
        "interactive_task_exists": True,
        "interactive_task_logon_type": "Interactive",
        "command_port_listening": True,
        "command_port_loopback_only": False,
        "command_port_addresses": ["0.0.0.0"],
        "command_port_processes": ["maya"],
        "command_port_process_paths": [config.sessiond.maya_exe],
    }
    runner = FakeRunner(
        [Result(0, "host maya-win", ""), Result(0, __import__("json").dumps(remote), "")]
    )
    payload = doctor(config, runner)
    command_port = next(
        check for check in payload["checks"] if check["name"] == "remote.command_port"
    )
    assert command_port["ok"] is False


def test_doctor_skips_remote_checks_when_ssh_is_missing(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mac_maya_dev.operations.shutil.which",
        lambda executable: None if executable == "ssh" else f"/usr/bin/{executable}",
    )
    runner = FakeRunner([])
    payload = doctor(config, runner)
    assert payload["ok"] is False
    assert payload["remote"] == {}
    assert runner.calls == []


def test_deploy_uploads_snapshot_and_selects_hash(config: Config) -> None:
    runner = FakeRunner(
        [
            Result(0, '{"ok":true,"path":"C:/maya-mcp-dev/incoming/bundle.zip"}', ""),
            Result(0, "", ""),
            Result(
                0,
                '{"ok":true,"reused":false,"content_hash":"abc","path":"C:/deploy/abc","file_count":2}',
                "",
            ),
        ]
    )
    payload = deploy(config, runner)
    assert payload["ok"] is True
    assert payload["source"] == str(config.local.source)
    assert runner.calls[1][0] == "scp"
    assert runner.calls[1][-1].startswith("maya-win:")
    assert "maya-mcp-" in runner.calls[1][-1]
    assert runner.calls[1][-1].endswith(".zip")
    finalize_script = decode_ssh_script(runner.calls[2])
    assert "[System.IO.File]::Replace" in finalize_script
    assert "[System.IO.Directory]::Move" in finalize_script
    assert "Assert-Deployment" in finalize_script
    assert "Get-FileHash" in finalize_script
    assert "git_dirty = $false" in finalize_script
    assert "Assert-Deployment $stage" in finalize_script
    assert "$currentTemp" in finalize_script


def test_deploy_cleans_partial_remote_upload(config: Config) -> None:
    runner = FakeRunner(
        [
            Result(0, '{"ok":true,"path":"C:/maya-mcp-dev/incoming/bundle.zip"}', ""),
            Result(1, "", "connection lost"),
            Result(0, "", ""),
        ]
    )
    with pytest.raises(MayaDevError, match="snapshot upload failed: connection lost"):
        deploy(config, runner)
    cleanup_script = decode_ssh_script(runner.calls[2])
    assert "Remove-Item" in cleanup_script
    assert "bundle.zip" in cleanup_script


def test_connect_execs_remote_mcp_when_sessiond_is_inactive(config: Config) -> None:
    runner = FakeRunner([], exec_code=7)
    assert connect(config, runner) == 7
    assert runner.exec_calls
    script = decode_ssh_script(runner.exec_calls[0])
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "$state.maya_pid" in script
    assert "$state.mcp_pid" in script
    assert "Get-CimInstance Win32_Process" in script
    assert "Test-SameExecutable" in script
    assert "gg_maya_sessiond.daemon" in script
    assert "Global\\mac_maya_dev_command_port_7001" in script
    assert "$modeMutex.ReleaseMutex()" in script
    assert "maya_mcp.server" in script
    assert "current.json" in script
    assert "PYTHONPATH" in script
    assert "Join-Path $current.path 'src'" in script
    assert "PYTHONDONTWRITEBYTECODE" in script
    assert " -B -c $mcpBootstrap" in script


def test_connect_configures_non_default_port(config: Config) -> None:
    changed = Config(
        path=config.path,
        local=config.local,
        remote=RemoteConfig(
            ssh_host=config.remote.ssh_host,
            deploy_root=config.remote.deploy_root,
            python=config.remote.python,
            mcp_module=config.remote.mcp_module,
            port=7002,
        ),
        sessiond=config.sessiond,
        windows=config.windows,
    )
    runner = FakeRunner([], exec_code=0)

    assert connect(changed, runner) == 0
    script = decode_ssh_script(runner.exec_calls[0])
    assert "Global\\mac_maya_dev_command_port_7002" in script
    assert "$env:MAYA_MCP_PORT = '7002'" in script
    assert "get_client().reconfigure" in script
    assert "runpy.run_module" in script


def test_session_commands_use_configured_paths(config: Config) -> None:
    runner = FakeRunner(
        [
            Result(
                0,
                '{"state":{"status":"running"},"derived_status":"running"}',
                "",
            ),
            Result(0, '{"ok":true,"state":{"status":"running"}}', ""),
            Result(0, '{"ok":true,"structured":{"scene":"ok"}}', ""),
        ]
    )
    assert session_status(config, runner)["state"]["status"] == "running"
    assert session_start(config, runner)["ok"] is True
    assert session_call(
        config,
        runner,
        tool="scene.info",
        pairs=[],
        input_json=None,
        list_tools=False,
        tool_help=False,
    )["ok"] is True

    start_script = decode_ssh_script(runner.calls[1])
    assert config.sessiond.maya_exe in start_script
    assert "current.path" in start_script
    assert "Global\\mac_maya_dev_command_port_7001" in start_script
    assert "$modeMutex.ReleaseMutex()" in start_script
    assert "Start-ScheduledTask" in start_script
    assert config.sessiond.interactive_task in start_script
    assert "$previousSessionId" in start_script
    assert "$candidate.call_server_ready" in start_script
    assert "$candidate.port -eq 7001" in start_script
    assert "config.json" in start_script
    assert "$sessionConfig.mcp_src" in start_script
    assert "$rightSource" in start_script
    assert "$rightMaya" in start_script
    assert "$rightMcpPython" in start_script
    assert "$rightMcpModule" in start_script
    assert "$rightSessiondPython" in start_script
    call_script = decode_ssh_script(runner.calls[2])
    assert "scene.info" in call_script


def test_session_status_accepts_canonical_inactive_json(config: Config) -> None:
    payload = {
        "state_dir": "C:/state",
        "has_state": True,
        "state": {"status": "stopped"},
        "derived_status": "stopped",
    }
    runner = FakeRunner([Result(1, __import__("json").dumps(payload), "")])
    assert session_status(config, runner) == payload


def test_session_status_redacts_daemon_call_token(config: Config) -> None:
    payload = {
        "state_dir": "C:/state",
        "state": {"status": "running", "call_token": "do-not-print"},
        "derived_status": "running",
    }
    runner = FakeRunner([Result(0, __import__("json").dumps(payload), "")])
    result = session_status(config, runner)
    assert result["state"]["call_token"] == "[redacted]"


def test_session_status_rejects_ssh_failure_with_parseable_stdout(config: Config) -> None:
    payload = {
        "state_dir": "C:/state",
        "state": {"status": "stopped"},
        "derived_status": "stopped",
    }
    runner = FakeRunner([Result(255, __import__("json").dumps(payload), "connection lost")])
    with pytest.raises(MayaDevError, match="connection lost"):
        session_status(config, runner)


def test_session_restart_stops_before_start(config: Config) -> None:
    runner = FakeRunner(
        [
            Result(0, '{"ok":true,"state":{"status":"stopped"}}', ""),
            Result(0, '{"ok":true,"state":{"status":"running"}}', ""),
        ]
    )
    payload = session_restart(config, runner)
    assert payload["ok"] is True
    assert "'stop'" in decode_ssh_script(runner.calls[0])
    assert "Start-ScheduledTask" in decode_ssh_script(runner.calls[1])


def test_session_restart_aborts_when_stop_fails(config: Config) -> None:
    runner = FakeRunner([Result(1, '{"ok":false,"error":"cleanup failed"}', "")])
    payload = session_restart(config, runner)
    assert payload["ok"] is False
    assert payload["started"] is None
    assert len(runner.calls) == 1


def test_session_call_validates_json(config: Config) -> None:
    with pytest.raises(MayaDevError, match="invalid JSON"):
        session_call(
            config,
            FakeRunner([]),
            tool="scene.info",
            pairs=[],
            input_json="{broken",
            list_tools=False,
            tool_help=False,
        )


def test_session_call_preserves_structured_failure(config: Config) -> None:
    payload = {"ok": False, "error": "Maya rejected the operation"}
    runner = FakeRunner([Result(1, __import__("json").dumps(payload), "")])
    result = session_call(
        config,
        runner,
        tool="scene.info",
        pairs=[],
        input_json=None,
        list_tools=False,
        tool_help=False,
    )
    assert result == payload
