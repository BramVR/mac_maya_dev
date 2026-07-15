"""Developer workflow operations."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .errors import MayaDevError
from .remote import (
    Result,
    Runner,
    parse_json_result,
    parse_last_json_object,
    powershell_literal,
    run_powershell,
    ssh_args,
)
from .snapshot import Snapshot, build_snapshot


def _require_success(result: Result, action: str) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise MayaDevError(f"{action} failed: {detail}")


def _current_path_script(config: Config) -> str:
    root = config.remote.deploy_root.rstrip("/\\")
    current = f"{root}/current.json"
    return f"""
$ErrorActionPreference = 'Stop'
$currentFile = {powershell_literal(current)}
if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) {{
    throw 'No deployment selected. Run maya-dev deploy first.'
}}
$current = Get-Content -Raw -LiteralPath $currentFile | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $current.path -PathType Container)) {{
    throw "Selected deployment is missing: $($current.path)"
}}
""".strip()


def doctor(config: Config, runner: Runner) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    executables: dict[str, str | None] = {}
    for executable in ("git", "ssh", "scp"):
        path = shutil.which(executable)
        executables[executable] = path
        checks.append({"name": f"local.{executable}", "ok": bool(path), "detail": path})

    source_ok = config.local.source.is_dir() and (config.local.source / ".git").exists()
    checks.append(
        {
            "name": "local.source",
            "ok": source_ok,
            "detail": str(config.local.source),
        }
    )

    if executables["ssh"] is None:
        checks.append(
            {
                "name": "local.ssh_config",
                "ok": False,
                "detail": "skipped: ssh is not installed",
            }
        )
        return {
            "ok": False,
            "config": str(config.path),
            "checks": checks,
            "remote": {},
        }

    ssh_config = runner.run([executables["ssh"], "-G", config.remote.ssh_host])
    checks.append(
        {
            "name": "local.ssh_config",
            "ok": ssh_config.returncode == 0,
            "detail": config.remote.ssh_host,
        }
    )

    script = f"""
$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort {config.remote.port} -ErrorAction SilentlyContinue)
$ownerProcesses = @($listeners | ForEach-Object {{ Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue }})
$ownerNames = @($ownerProcesses | Select-Object -ExpandProperty ProcessName -Unique)
$ownerPaths = @($ownerProcesses | Select-Object -ExpandProperty Path -Unique)
$listenAddresses = @($listeners | Select-Object -ExpandProperty LocalAddress -Unique)
$loopbackOnly = $listeners.Count -gt 0
foreach ($address in $listenAddresses) {{
    if ($address -notin @('127.0.0.1', '::1')) {{
        $loopbackOnly = $false
    }}
}}
$pythonVersion = $null
$pythonOk = $false
if (Test-Path -LiteralPath {powershell_literal(config.remote.python)} -PathType Leaf) {{
    $pythonVersion = (& {powershell_literal(config.remote.python)} --version 2>&1 | Out-String).Trim()
    $pythonOk = ($LASTEXITCODE -eq 0)
}}
$sessiondVersion = $null
$sessiondOk = $false
if (Test-Path -LiteralPath {powershell_literal(config.sessiond.python)} -PathType Leaf) {{
    $sessiondVersion = (& {powershell_literal(config.sessiond.python)} -m {powershell_literal(config.sessiond.module)} --version 2>&1 | Out-String).Trim()
    $sessiondOk = ($LASTEXITCODE -eq 0)
}}
$interactiveTask = Get-ScheduledTask -TaskName {powershell_literal(config.sessiond.interactive_task)} -ErrorAction SilentlyContinue
$interactiveTaskLogonType = $null
$interactiveTaskState = $null
if ($interactiveTask) {{
    $interactiveTaskLogonType = [string]$interactiveTask.Principal.LogonType
    $interactiveTaskState = [string]$interactiveTask.State
}}
[ordered]@{{
    ok = $true
    python_exists = [bool](Test-Path -LiteralPath {powershell_literal(config.remote.python)} -PathType Leaf)
    python_runnable = $pythonOk
    python_version = $pythonVersion
    maya_exists = [bool](Test-Path -LiteralPath {powershell_literal(config.sessiond.maya_exe)} -PathType Leaf)
    sessiond_exists = [bool](Test-Path -LiteralPath {powershell_literal(config.sessiond.python)} -PathType Leaf)
    sessiond_importable = $sessiondOk
    sessiond_version = $sessiondVersion
    interactive_task_exists = [bool]$interactiveTask
    interactive_task_logon_type = $interactiveTaskLogonType
    interactive_task_state = $interactiveTaskState
    deploy_root_exists = [bool](Test-Path -LiteralPath {powershell_literal(config.remote.deploy_root)} -PathType Container)
    command_port = {config.remote.port}
    command_port_listening = $listeners.Count -gt 0
    command_port_loopback_only = $loopbackOnly
    command_port_addresses = $listenAddresses
    command_port_processes = $ownerNames
    command_port_process_paths = $ownerPaths
}} | ConvertTo-Json -Compress
""".strip()
    try:
        remote = parse_json_result(
            run_powershell(runner, config.remote.ssh_host, script), action="remote doctor"
        )
        raw_owner_paths = remote.get("command_port_process_paths")
        if isinstance(raw_owner_paths, str):
            owner_paths = [raw_owner_paths]
        elif isinstance(raw_owner_paths, list):
            owner_paths = [str(path) for path in raw_owner_paths]
        else:
            owner_paths = []
        expected_owner = config.sessiond.maya_exe
        expected_normalized = expected_owner.replace("\\", "/").casefold()
        owner_matches = bool(owner_paths) and all(
            path.replace("\\", "/").casefold() == expected_normalized for path in owner_paths
        )
        checks.extend(
            [
                {
                    "name": "remote.python",
                    "ok": bool(remote.get("python_runnable")),
                    "detail": remote.get("python_version"),
                },
                {
                    "name": "remote.maya",
                    "ok": bool(remote.get("maya_exists")),
                    "detail": config.sessiond.maya_exe,
                },
                {
                    "name": "remote.sessiond",
                    "ok": bool(remote.get("sessiond_importable")),
                    "detail": remote.get("sessiond_version"),
                },
                {
                    "name": "remote.interactive_task",
                    "ok": bool(remote.get("interactive_task_exists"))
                    and remote.get("interactive_task_logon_type") == "Interactive",
                    "detail": {
                        "name": config.sessiond.interactive_task,
                        "logon_type": remote.get("interactive_task_logon_type"),
                        "state": remote.get("interactive_task_state"),
                    },
                },
                {
                    "name": "remote.command_port",
                    "ok": bool(remote.get("command_port_listening"))
                    and bool(remote.get("command_port_loopback_only"))
                    and owner_matches,
                    "detail": {
                        "addresses": remote.get("command_port_addresses"),
                        "processes": remote.get("command_port_processes"),
                        "paths": owner_paths,
                        "expected": expected_owner,
                    },
                },
            ]
        )
    except MayaDevError as exc:
        checks.append({"name": "remote.ssh", "ok": False, "detail": str(exc)})
        remote = {}

    return {
        "ok": all(bool(check["ok"]) for check in checks),
        "config": str(config.path),
        "checks": checks,
        "remote": remote,
    }


def check_source(config: Config, runner: Runner) -> dict[str, Any]:
    if not config.local.check_commands:
        raise MayaDevError("No [local].check_commands configured")
    results: list[dict[str, Any]] = []
    for command in config.local.check_commands:
        result = runner.run(command, cwd=str(config.local.source))
        item = {
            "command": list(command),
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        results.append(item)
        if result.returncode != 0:
            break
    return {"ok": all(bool(item["ok"]) for item in results), "results": results}


def _prepare_upload(config: Config, runner: Runner, filename: str) -> str:
    root = config.remote.deploy_root.rstrip("/\\")
    incoming_dir = f"{root}/incoming"
    incoming_path = f"{incoming_dir}/{filename}"
    script = f"""
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path {powershell_literal(incoming_dir)} | Out-Null
[ordered]@{{ok=$true; path={powershell_literal(incoming_path)}}} | ConvertTo-Json -Compress
""".strip()
    payload = parse_json_result(
        run_powershell(runner, config.remote.ssh_host, script), action="prepare upload"
    )
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise MayaDevError("prepare upload did not return a path")
    return path


def _finish_deploy(config: Config, runner: Runner, snapshot: Snapshot, incoming: str) -> dict[str, Any]:
    root = config.remote.deploy_root.rstrip("/\\")
    destination = f"{root}/deployments/{snapshot.content_hash}"
    current_file = f"{root}/current.json"
    stage = f"{root}/deployments/.staging-{snapshot.content_hash}-{uuid.uuid4().hex}"
    script = f"""
$ErrorActionPreference = 'Stop'
$incoming = {powershell_literal(incoming)}
$destination = {powershell_literal(destination)}
$stage = {powershell_literal(stage)}
$currentFile = {powershell_literal(current_file)}
$currentTemp = "$currentFile.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
function Assert-Deployment([string]$path, [string]$expectedHash) {{
    $manifestPath = Join-Path $path '.maya-dev-deployment.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {{
        throw "Deployment is missing its manifest: $path"
    }}
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.content_hash -ne $expectedHash) {{
        throw "Deployment manifest hash mismatch: $path"
    }}
    $expectedFiles = @($manifest.files.PSObject.Properties)
    $actualFiles = @(Get-ChildItem -LiteralPath $path -Recurse -File | Where-Object {{ $_.FullName -ne $manifestPath }})
    if ($expectedFiles.Count -ne $actualFiles.Count) {{
        throw "Deployment file count mismatch: $path"
    }}
    $root = [System.IO.Path]::GetFullPath($path).TrimEnd('\\') + '\\'
    foreach ($entry in $expectedFiles) {{
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $path $entry.Name.Replace('/', '\\')))
        if (-not $candidate.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {{
            throw "Unsafe path in deployment manifest: $($entry.Name)"
        }}
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {{
            throw "Deployment file is missing: $($entry.Name)"
        }}
        $actualHash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash
        if ($actualHash -ine [string]$entry.Value) {{
            throw "Deployment file hash mismatch: $($entry.Name)"
        }}
    }}
}}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
try {{
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) {{
        if (Test-Path -LiteralPath $stage) {{ Remove-Item -LiteralPath $stage -Recurse -Force }}
        New-Item -ItemType Directory -Path $stage | Out-Null
        Expand-Archive -LiteralPath $incoming -DestinationPath $stage -Force
        Assert-Deployment $stage {powershell_literal(snapshot.content_hash)}
        try {{
            [System.IO.Directory]::Move($stage, $destination)
            $reused = $false
        }} catch [System.IO.IOException] {{
            if (Test-Path -LiteralPath $destination -PathType Container) {{
                $reused = $true
            }} else {{
                throw
            }}
        }}
    }} else {{
        $reused = $true
    }}
    Assert-Deployment $destination {powershell_literal(snapshot.content_hash)}
    $current = [ordered]@{{
        schema = 1
        content_hash = {powershell_literal(snapshot.content_hash)}
        path = $destination
        git_commit = {powershell_literal(snapshot.git_commit or '')}
        git_head = {powershell_literal(snapshot.git_head or '')}
        git_dirty = ${str(snapshot.git_dirty).lower()}
        file_count = {snapshot.file_count}
        selected_at = [DateTime]::UtcNow.ToString('o')
    }}
    $current | ConvertTo-Json | Set-Content -LiteralPath $currentTemp -Encoding UTF8
    if (Test-Path -LiteralPath $currentFile -PathType Leaf) {{
        [System.IO.File]::Replace($currentTemp, $currentFile, $null)
    }} else {{
        [System.IO.File]::Move($currentTemp, $currentFile)
    }}
    [ordered]@{{ok=$true; reused=$reused; content_hash=$current.content_hash; path=$destination; file_count=$current.file_count}} | ConvertTo-Json -Compress
}} finally {{
    if (Test-Path -LiteralPath $currentTemp) {{ Remove-Item -LiteralPath $currentTemp -Force }}
    if (Test-Path -LiteralPath $incoming) {{ Remove-Item -LiteralPath $incoming -Force }}
    if (Test-Path -LiteralPath $stage) {{ Remove-Item -LiteralPath $stage -Recurse -Force }}
}}
""".strip()
    return parse_json_result(
        run_powershell(runner, config.remote.ssh_host, script), action="finish deployment"
    )


def deploy(config: Config, runner: Runner) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="maya-dev-") as temp:
        snapshot = build_snapshot(config.local.source, Path(temp))
        incoming_name = f"{snapshot.archive.stem}-{uuid.uuid4().hex}.zip"
        incoming = _prepare_upload(config, runner, incoming_name)
        remote_target = f"{config.remote.ssh_host}:{incoming}"
        upload = runner.run(
            ["scp", "-q", "-o", "BatchMode=yes", str(snapshot.archive), remote_target]
        )
        if upload.returncode != 0:
            cleanup_script = f"""
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath {powershell_literal(incoming)}) {{
    Remove-Item -LiteralPath {powershell_literal(incoming)} -Force
}}
""".strip()
            cleanup = run_powershell(runner, config.remote.ssh_host, cleanup_script)
            detail = upload.stderr.strip() or upload.stdout.strip() or f"exit {upload.returncode}"
            if cleanup.returncode != 0:
                cleanup_detail = cleanup.stderr.strip() or f"exit {cleanup.returncode}"
                detail += f"; remote cleanup also failed: {cleanup_detail}"
            raise MayaDevError(f"snapshot upload failed: {detail}")
        payload = _finish_deploy(config, runner, snapshot, incoming)
    payload.update(
        {
            "git_commit": snapshot.git_commit,
            "git_head": snapshot.git_head,
            "git_dirty": snapshot.git_dirty,
            "source": str(config.local.source),
        }
    )
    return payload


def _mode_mutex_name(config: Config) -> str:
    return f"Global\\mac_maya_dev_command_port_{config.remote.port}"


def _mode_mutex_open(config: Config) -> str:
    return f"""
$modeMutex = [System.Threading.Mutex]::new($false, {powershell_literal(_mode_mutex_name(config))})
$modeMutexAcquired = $false
try {{
    try {{
        $modeMutexAcquired = $modeMutex.WaitOne(0)
    }} catch [System.Threading.AbandonedMutexException] {{
        $modeMutexAcquired = $true
    }}
    if (-not $modeMutexAcquired) {{
        throw 'Another mac_maya_dev MCP mode is active for this command port.'
    }}
"""


def _mode_mutex_close() -> str:
    return """
} finally {
    if ($modeMutexAcquired) { $modeMutex.ReleaseMutex() }
    $modeMutex.Dispose()
}
"""


def _sessiond_inactive_guard(config: Config) -> str:
    state_root = config.sessiond.state_dir.rstrip("/\\")
    state_file = f"{state_root}/state.json"
    return f"""
$active = $false
function Test-SameExecutable([string]$actual, [string]$expected) {{
    if (-not $actual -or -not $expected) {{ return $false }}
    return $actual.Replace('/', '\\').TrimEnd('\\') -ieq $expected.Replace('/', '\\').TrimEnd('\\')
}}
function Test-CommandContains([string]$commandLine, [string]$expected) {{
    if (-not $commandLine -or -not $expected) {{ return $false }}
    return $commandLine.IndexOf($expected, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}}
if (Test-Path -LiteralPath {powershell_literal(state_file)} -PathType Leaf) {{
    $state = Get-Content -Raw -LiteralPath {powershell_literal(state_file)} | ConvertFrom-Json
    $daemonProcess = $null
    if ($state.daemon_pid) {{
        $daemonProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.daemon_pid)" -ErrorAction SilentlyContinue
    }}
    if ($daemonProcess -and
        (Test-SameExecutable $daemonProcess.ExecutablePath {powershell_literal(config.sessiond.python)}) -and
        (Test-CommandContains $daemonProcess.CommandLine 'gg_maya_sessiond.daemon') -and
        (Test-CommandContains $daemonProcess.CommandLine {powershell_literal(config.sessiond.state_dir)})) {{
        $active = $true
    }}
    $mayaProcess = $null
    if ($state.maya_pid) {{
        $mayaProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.maya_pid)" -ErrorAction SilentlyContinue
    }}
    if ($mayaProcess -and
        (Test-SameExecutable $mayaProcess.ExecutablePath {powershell_literal(config.sessiond.maya_exe)})) {{
        $active = $true
    }}
    $mcpProcess = $null
    if ($state.mcp_pid) {{
        $mcpProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.mcp_pid)" -ErrorAction SilentlyContinue
    }}
    if ($mcpProcess -and
        (Test-SameExecutable $mcpProcess.ExecutablePath {powershell_literal(config.remote.python)}) -and
        (Test-CommandContains $mcpProcess.CommandLine {powershell_literal(config.remote.mcp_module)})) {{
        $active = $true
    }}
}}
if ($active) {{
    throw 'sessiond still owns a live daemon, Maya, or MCP process.'
}}
""".strip()


def connect(config: Config, runner: Runner) -> int:
    """Run the selected Windows deployment as a stdio MCP server over SSH."""
    script = "$ErrorActionPreference = 'Stop'\n"
    script += _mode_mutex_open(config)
    script += _sessiond_inactive_guard(config) + "\n"
    script += _current_path_script(config) + f"""
$sourcePath = Join-Path $current.path 'src'
$env:PYTHONPATH = "$sourcePath;$($current.path);$env:PYTHONPATH"
$env:MAYA_MCP_PORT = {powershell_literal(str(config.remote.port))}
$env:MAYA_MCP_MODULE = {powershell_literal(config.remote.mcp_module)}
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
Set-Location -LiteralPath $current.path
$mcpBootstrap = @'
import os
import runpy

from maya_mcp.transport import get_client

get_client().reconfigure(port=int(os.environ["MAYA_MCP_PORT"]))
runpy.run_module(os.environ["MAYA_MCP_MODULE"], run_name="__main__", alter_sys=True)
'@
& {powershell_literal(config.remote.python)} -B -c $mcpBootstrap
$commandExitCode = $LASTEXITCODE
"""
    script += _mode_mutex_close()
    script += "exit $commandExitCode\n"
    return runner.exec(ssh_args(config.remote.ssh_host, script))


def _redact_session_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key == "call_token" else _redact_session_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_session_value(child) for child in value]
    return value


def _redact_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_session_value(value) for key, value in payload.items()}


def _session_command(
    config: Config,
    runner: Runner,
    command: str,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    args = [command, "--state-dir", config.sessiond.state_dir]
    if extra:
        args.extend(extra)
    args.append("--json")

    rendered: list[str] = []
    for arg in args:
        rendered.append(arg if arg == "$($current.path)" else powershell_literal(arg))
    script = "$ErrorActionPreference = 'Stop'\n" + f"""
& {powershell_literal(config.sessiond.python)} -m {powershell_literal(config.sessiond.module)} {' '.join(rendered)}
$commandExitCode = $LASTEXITCODE
"""
    script += "exit $commandExitCode\n"
    result = run_powershell(runner, config.remote.ssh_host, script)
    if command == "status":
        if result.returncode not in {0, 1}:
            _require_success(result, "sessiond status")
        payload = parse_last_json_object(result.stdout, action="sessiond status")
        if isinstance(payload, dict) and isinstance(payload.get("derived_status"), str):
            active = payload["derived_status"] in {"running", "starting", "stopping"}
            if (result.returncode == 0) == active:
                return _redact_session_payload(payload)
            raise MayaDevError("sessiond status exit code does not match its reported state")
        _require_success(result, "sessiond status")
        raise MayaDevError("sessiond status returned an unexpected JSON value")
    if command in {"call", "stop"} and result.returncode in {0, 1}:
        payload = parse_last_json_object(result.stdout, action=f"sessiond {command}")
        if (result.returncode == 0) != bool(payload.get("ok")):
            raise MayaDevError(f"sessiond {command} exit code does not match its result")
    else:
        payload = parse_json_result(result, action=f"sessiond {command}")
    return _redact_session_payload(payload) if command == "stop" else payload


def session_status(config: Config, runner: Runner) -> dict[str, Any]:
    return _session_command(config, runner, "status")


def session_start(config: Config, runner: Runner) -> dict[str, Any]:
    state_root = config.sessiond.state_dir.rstrip("/\\")
    state_file = f"{state_root}/state.json"
    script = "$ErrorActionPreference = 'Stop'\n"
    script += _mode_mutex_open(config)
    script += _sessiond_inactive_guard(config) + "\n"
    script += _current_path_script(config) + f"""
$stateFile = {powershell_literal(state_file)}
$sessionConfigFile = Join-Path {powershell_literal(state_root)} 'config.json'
$selectedMcpPath = [System.IO.Path]::GetFullPath([string]$current.path).TrimEnd('\\')
$previousSessionId = $null
if (Test-Path -LiteralPath $stateFile -PathType Leaf) {{
    $previousState = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
    $previousSessionId = $previousState.session_id
}}
$task = Get-ScheduledTask -TaskName {powershell_literal(config.sessiond.interactive_task)} -ErrorAction Stop
if ([string]$task.Principal.LogonType -ne 'Interactive') {{
    throw 'Configured sessiond task must use the Interactive logon type.'
}}
Start-ScheduledTask -InputObject $task | Out-Null
$deadline = [DateTime]::UtcNow.AddSeconds({config.sessiond.wait_timeout_seconds})
$startedState = $null
do {{
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $stateFile -PathType Leaf) {{
        $candidate = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
        $isNewSession = $candidate.session_id -and $candidate.session_id -ne $previousSessionId
        $rightPort = $candidate.port -eq {config.remote.port}
        $rightSource = $false
        $rightMaya = $false
        $rightMcpPython = $false
        $rightMcpModule = $false
        if (Test-Path -LiteralPath $sessionConfigFile -PathType Leaf) {{
            $sessionConfig = Get-Content -Raw -LiteralPath $sessionConfigFile | ConvertFrom-Json
            if ($sessionConfig.mcp_src) {{
                $sessionMcpPath = [System.IO.Path]::GetFullPath([string]$sessionConfig.mcp_src).TrimEnd('\\')
                $rightSource = $sessionMcpPath -ieq $selectedMcpPath
            }}
            $rightMaya = Test-SameExecutable ([string]$sessionConfig.maya_exe) {powershell_literal(config.sessiond.maya_exe)}
            $rightMcpPython = Test-SameExecutable ([string]$sessionConfig.mcp_python) {powershell_literal(config.remote.python)}
            $rightMcpModule = [string]$sessionConfig.mcp_module -eq {powershell_literal(config.remote.mcp_module)}
        }}
        $daemonProcess = $null
        if ($candidate.daemon_pid) {{
            $daemonProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.daemon_pid)" -ErrorAction SilentlyContinue
        }}
        $rightSessiondPython = $daemonProcess -and (Test-SameExecutable $daemonProcess.ExecutablePath {powershell_literal(config.sessiond.python)})
        $rightRuntime = $rightSource -and $rightMaya -and $rightMcpPython -and $rightMcpModule -and $rightSessiondPython
        if ($isNewSession -and $rightPort -and $rightRuntime -and $candidate.status -eq 'running' -and $candidate.call_server_ready) {{
            $startedState = $candidate
            break
        }}
        if ($isNewSession -and $candidate.status -in @('failed', 'stopped')) {{
            throw "Interactive session failed: $($candidate.error)"
        }}
    }}
}} while ([DateTime]::UtcNow -lt $deadline)
if (-not $startedState) {{
    throw 'Interactive session did not reach running state before timeout.'
}}
$publicState = [ordered]@{{
    status = $startedState.status
    updated_at = $startedState.updated_at
    session_id = $startedState.session_id
    daemon_pid = $startedState.daemon_pid
    maya_pid = $startedState.maya_pid
    mcp_pid = $startedState.mcp_pid
    port = $startedState.port
    call_server_ready = $startedState.call_server_ready
}}
[ordered]@{{ok=$true; task={powershell_literal(config.sessiond.interactive_task)}; state=$publicState}} | ConvertTo-Json -Compress -Depth 4
$commandExitCode = 0
"""
    script += _mode_mutex_close()
    script += "exit $commandExitCode\n"
    return parse_json_result(
        run_powershell(runner, config.remote.ssh_host, script), action="interactive session start"
    )


def session_stop(config: Config, runner: Runner) -> dict[str, Any]:
    return _session_command(config, runner, "stop")


def session_restart(config: Config, runner: Runner) -> dict[str, Any]:
    stopped = session_stop(config, runner)
    if not stopped.get("ok"):
        return {"ok": False, "stopped": stopped, "started": None}
    started = session_start(config, runner)
    return {"ok": bool(started.get("ok")), "stopped": stopped, "started": started}


def session_call(
    config: Config,
    runner: Runner,
    *,
    tool: str | None,
    pairs: list[str],
    input_json: str | None,
    list_tools: bool,
    tool_help: bool,
) -> dict[str, Any]:
    extra: list[str] = []
    if list_tools:
        extra.append("--list")
    if tool_help:
        extra.append("--tool-help")
    if input_json is not None:
        try:
            json.loads(input_json)
        except json.JSONDecodeError as exc:
            raise MayaDevError(f"--input-json is invalid JSON: {exc}") from exc
        extra.extend(["--input-json", input_json])
    if tool:
        extra.append(tool)
    extra.extend(pairs)
    return _session_command(config, runner, "call", extra)
