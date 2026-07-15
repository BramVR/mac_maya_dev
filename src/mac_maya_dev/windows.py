"""Read-only inspection and explicit Windows host setup."""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Config, validate_windows_layout
from .errors import MayaDevError
from .remote import (
    Result,
    Runner,
    parse_json_result,
    parse_last_json_object,
    powershell_literal,
    run_powershell,
    run_powershell_with_stdin,
)

ASSET_NAMES = (
    "check-maya2024.ps1",
    "setup-maya2024.ps1",
    "start-sessiond-maya2024.ps1",
    "mcp-requirements-py311.lock",
    "setup-manifest.json",
)

SETUP_OWNED_CHECKS = {
    "python.base",
    "mcp.runtime",
    "mcp.lock",
    "mcp.dependencies",
    "launcher.content",
    "task.interactive",
}
BLOCKING_CHECKS = {
    "host.console_user",
    "maya.executable",
    "sessiond.runtime",
    "port.command",
}
INFORMATIONAL_CHECKS = {"host.git", "sessiond.reproducibility"}


def _asset_bytes(name: str) -> bytes:
    path = Path(__file__).resolve().parents[2] / "windows" / name
    if path.is_file():
        return path.read_bytes()
    resource = importlib.resources.files("mac_maya_dev").joinpath("windows", name)
    try:
        return resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise MayaDevError(f"Packaged Windows setup asset is missing: {name}") from exc


def _asset_text(name: str) -> str:
    return _asset_bytes(name).decode("utf-8")


def _asset_sha256(name: str) -> str:
    return hashlib.sha256(_asset_bytes(name)).hexdigest()


def _locked_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for name, version in re.findall(
        r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)",
        _asset_text("mcp-requirements-py311.lock"),
    ):
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        packages[normalized] = version
    if not packages:
        raise MayaDevError("Windows MCP lock contains no pinned packages")
    return packages


def _manifest() -> dict[str, Any]:
    try:
        payload = json.loads(_asset_text("setup-manifest.json"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MayaDevError(f"Invalid Windows setup manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise MayaDevError("Invalid Windows setup manifest schema")
    lock = payload.get("mcp_lock")
    python = payload.get("python")
    launcher = payload.get("launcher")
    if not all(isinstance(item, dict) for item in (lock, python, launcher)):
        raise MayaDevError("Windows setup manifest is incomplete")
    assert isinstance(lock, dict) and isinstance(python, dict) and isinstance(launcher, dict)
    if lock.get("file") != "mcp-requirements-py311.lock":
        raise MayaDevError("Windows setup manifest has an unexpected MCP lock filename")
    if launcher.get("file") != "start-sessiond-maya2024.ps1":
        raise MayaDevError("Windows setup manifest has an unexpected launcher filename")
    python_url = python.get("url")
    if not isinstance(python_url, str):
        raise MayaDevError("Windows setup manifest has no Python installer URL")
    if python.get("architecture") != "AMD64" or python.get("bits") != 64:
        raise MayaDevError("Windows Python installer must be pinned to 64-bit AMD64")
    parsed_url = urlparse(python_url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "www.python.org":
        raise MayaDevError("Windows Python installer must use the official python.org source")
    if _asset_sha256(str(lock.get("file"))) != lock.get("sha256"):
        raise MayaDevError("Windows MCP lock does not match its setup manifest")
    if _asset_sha256(str(launcher.get("file"))) != launcher.get("sha256"):
        raise MayaDevError("Windows launcher does not match its setup manifest")
    return payload


def task_command(config: Config) -> tuple[str, str]:
    """Return the scheduled-task executable and injection-safe encoded arguments."""
    if config.windows is None:
        raise MayaDevError("Windows setup requires a [windows] config section")
    validate_windows_layout(config.windows, config.remote, config.sessiond)
    if not config.sessiond.reuse_existing:
        raise MayaDevError(
            "Windows setup requires explicit [sessiond].reuse_existing = true"
        )
    root = config.remote.deploy_root.rstrip("/\\")
    values = [
        ("CurrentFile", f"{root}/current.json"),
        ("SessiondPython", config.sessiond.python),
        ("StateDir", config.sessiond.state_dir),
        ("MayaExe", config.sessiond.maya_exe),
        ("McpPython", config.remote.python),
        ("Port", str(config.remote.port)),
        ("WaitTimeoutSeconds", str(config.sessiond.wait_timeout_seconds)),
        ("SessiondModule", config.sessiond.module),
        ("McpModule", config.remote.mcp_module),
    ]
    script = f"& {powershell_literal(config.windows.launcher)}"
    for name, value in values:
        script += f" -{name} {powershell_literal(value)}"
    script += "\nexit $LASTEXITCODE\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    arguments = (
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded}"
    )
    return "powershell.exe", arguments


def _check_parameters(config: Config) -> dict[str, Any]:
    if config.windows is None:
        raise MayaDevError("Windows setup requires a [windows] config section")
    manifest = _manifest()
    python = manifest["python"]
    lock = manifest["mcp_lock"]
    launcher = manifest["launcher"]
    _, task_arguments = task_command(config)
    return {
        "SetupRoot": config.windows.setup_root,
        "PythonInstallDir": config.windows.python_install_dir,
        "McpVenvDir": config.windows.mcp_venv_dir,
        "McpPython": config.remote.python,
        "McpModule": config.remote.mcp_module,
        "DeployRoot": config.remote.deploy_root,
        "SessiondPython": config.sessiond.python,
        "SessiondStateDir": config.sessiond.state_dir,
        "MayaExe": config.sessiond.maya_exe,
        "SessiondModule": config.sessiond.module,
        "Launcher": config.windows.launcher,
        "InteractiveTask": config.sessiond.interactive_task,
        "InteractiveUser": config.windows.interactive_user,
        "Port": config.remote.port,
        "ExpectedPythonVersion": python["version"],
        "ExpectedPythonArchitecture": python["architecture"],
        "ExpectedPythonBits": python["bits"],
        "ExpectedMcpLockHash": lock["sha256"],
        "ExpectedMcpPackagesJson": json.dumps(
            _locked_packages(), sort_keys=True, separators=(",", ":")
        ),
        "ExpectedLauncherHash": launcher["sha256"],
        "ExpectedTaskArguments": task_arguments,
    }


def _stdin_invocation(script: str, parameters: dict[str, Any]) -> tuple[str, str]:
    payload = json.dumps(
        {"parameters": parameters, "script": script}, separators=(",", ":")
    )
    bootstrap = """
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$parameters = @{}
$payload.parameters.PSObject.Properties | ForEach-Object { $parameters[$_.Name] = $_.Value }
$scriptBlock = [ScriptBlock]::Create([string]$payload.script)
& $scriptBlock @parameters
""".strip()
    return bootstrap, payload


def windows_check(config: Config, runner: Runner) -> dict[str, Any]:
    bootstrap, payload = _stdin_invocation(
        _asset_text("check-maya2024.ps1"), _check_parameters(config)
    )
    return parse_json_result(
        run_powershell_with_stdin(
            runner, config.remote.ssh_host, bootstrap, payload
        ),
        action="Windows host check",
    )


def _plan(check: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    raw_checks = check.get("checks")
    if not isinstance(raw_checks, list):
        raise MayaDevError("Windows host check returned no checks")
    failed = {
        str(item.get("id")): item
        for item in raw_checks
        if isinstance(item, dict) and not bool(item.get("ok"))
    }
    blockers = [
        {"id": check_id, "remediation": str(failed[check_id].get("remediation", ""))}
        for check_id in sorted(BLOCKING_CHECKS & failed.keys())
    ]
    actions: list[dict[str, str]] = []
    failed_directories = sorted(
        check_id for check_id in failed if check_id.startswith("directory.")
    )
    missing_directories: list[str] = []
    for check_id in failed_directories:
        actual = failed[check_id].get("actual")
        if isinstance(actual, dict) and not bool(actual.get("exists")):
            missing_directories.append(check_id)
        else:
            blockers.append(
                {
                    "id": check_id,
                    "remediation": str(failed[check_id].get("remediation", "")),
                }
            )
    if missing_directories:
        actions.append(
            {
                "id": "directories",
                "action": "create",
                "detail": ", ".join(missing_directories),
            }
        )
    if "python.base" in failed:
        python_actual = failed["python.base"].get("actual")
        python_dir_exists = not isinstance(python_actual, dict) or bool(
            python_actual.get("directory_exists")
        )
        if python_dir_exists:
            blockers.append(
                {
                    "id": "python.base",
                    "remediation": str(failed["python.base"].get("remediation", "")),
                }
            )
        else:
            actions.append(
                {"id": "python.base", "action": "install", "detail": "pinned CPython"}
            )
    mcp_rebuild = bool(
        {"mcp.runtime", "mcp.lock", "mcp.dependencies"} & failed.keys()
    )
    if mcp_rebuild:
        actions.append(
            {"id": "mcp.runtime", "action": "rebuild", "detail": "isolated hashed lock"}
        )
    if "launcher.content" in failed:
        launcher_actual = failed["launcher.content"].get("actual")
        launcher_occupied = not isinstance(launcher_actual, dict) or (
            bool(launcher_actual.get("exists"))
            and launcher_actual.get("kind") != "file"
        )
        if launcher_occupied:
            blockers.append(
                {
                    "id": "launcher.content",
                    "remediation": str(
                        failed["launcher.content"].get("remediation", "")
                    ),
                }
            )
        else:
            actions.append(
                {
                    "id": "launcher.content",
                    "action": "install",
                    "detail": "repository launcher",
                }
            )
    if "task.interactive" in failed:
        actions.append(
            {"id": "task.interactive", "action": "register", "detail": "passwordless task"}
        )
    unexpected = sorted(
        check_id
        for check_id in failed
        if check_id not in SETUP_OWNED_CHECKS
        and check_id not in BLOCKING_CHECKS
        and check_id not in INFORMATIONAL_CHECKS
        and not (check_id == "mcp.source_import" and mcp_rebuild)
        and not check_id.startswith("directory.")
    )
    blockers.extend(
        {"id": check_id, "remediation": "Resolve failed prerequisite."}
        for check_id in unexpected
    )
    return actions, blockers


def _setup_payload(
    check: dict[str, Any], actions: list[dict[str, str]], blockers: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": "windows.setup",
        "mode": "dry-run",
        "ok": not blockers,
        "changed": False,
        "actions": actions,
        "blockers": blockers,
        "check": check,
    }


def _write_bundle(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in ASSET_NAMES:
            bundle.writestr(name, _asset_bytes(name))


def _prepare_remote_stage(config: Config, runner: Runner, filename: str) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Stop'
$stageName = 'mac-maya-dev-setup-' + [Guid]::NewGuid().ToString('N')
$stage = Join-Path ([IO.Path]::GetTempPath()) $stageName
New-Item -ItemType Directory -Path $stage | Out-Null
$bundle = Join-Path $stage {powershell_literal(filename)}
[ordered]@{{
    ok=$true
    stage=$stage.Replace([char]92, [char]47)
    bundle=$bundle.Replace([char]92, [char]47)
}} | ConvertTo-Json -Compress
""".strip()
    return parse_json_result(
        run_powershell(runner, config.remote.ssh_host, script), action="prepare Windows setup"
    )


def _cleanup_remote_stage(config: Config, runner: Runner, stage: str) -> Result:
    script = f"""
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath {powershell_literal(stage)} -PathType Container) {{
    Remove-Item -LiteralPath {powershell_literal(stage)} -Recurse -Force
}}
""".strip()
    return run_powershell(runner, config.remote.ssh_host, script)


def _apply_setup(
    config: Config, runner: Runner, actions: list[dict[str, str]]
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="maya-dev-windows-") as temp:
        bundle_path = Path(temp) / f"setup-{uuid.uuid4().hex}.zip"
        _write_bundle(bundle_path)
        stage_payload = _prepare_remote_stage(config, runner, bundle_path.name)
        stage = stage_payload.get("stage")
        remote_bundle = stage_payload.get("bundle")
        if not isinstance(stage, str) or not isinstance(remote_bundle, str):
            raise MayaDevError("prepare Windows setup returned invalid paths")
        upload = runner.run(
            [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                str(bundle_path),
                f"{config.remote.ssh_host}:{remote_bundle}",
            ]
        )
        if upload.returncode != 0:
            cleanup = _cleanup_remote_stage(config, runner, stage)
            detail = upload.stderr.strip() or upload.stdout.strip() or f"exit {upload.returncode}"
            if cleanup.returncode != 0:
                cleanup_detail = cleanup.stderr.strip() or f"exit {cleanup.returncode}"
                detail += f"; remote cleanup also failed: {cleanup_detail}"
            raise MayaDevError(f"Windows setup upload failed: {detail}")

        apply_parameters = _check_parameters(config)
        for key in (
            "McpModule",
            "ExpectedPythonVersion",
            "ExpectedPythonArchitecture",
            "ExpectedPythonBits",
            "ExpectedMcpLockHash",
            "ExpectedLauncherHash",
        ):
            apply_parameters.pop(key)
        apply_parameters["BundleDir"] = f"{stage}/assets"
        apply_input = json.dumps(
            {
                "parameters": apply_parameters,
                "stage": stage,
                "bundle": remote_bundle,
            },
            separators=(",", ":"),
        )
        bootstrap = """
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$stage = [string]$payload.stage
$exitCode = 1
try {
    $assets = Join-Path $stage 'assets'
    Expand-Archive -LiteralPath ([string]$payload.bundle) -DestinationPath $assets
    $parameters = @{}
    $payload.parameters.PSObject.Properties | ForEach-Object { $parameters[$_.Name] = $_.Value }
    try {
        & (Join-Path $assets 'setup-maya2024.ps1') @parameters
        $exitCode = 0
    } catch {
        $exitCode = 1
    }
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
exit $exitCode
""".strip()
        try:
            result = run_powershell_with_stdin(
                runner, config.remote.ssh_host, bootstrap, apply_input
            )
            if result.returncode not in {0, 1}:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit {result.returncode}"
                )
                raise MayaDevError(f"Windows setup failed: {detail}")
            try:
                payload = parse_last_json_object(result.stdout, action="Windows setup")
            except MayaDevError as exc:
                detail = result.stderr.strip()
                if detail:
                    raise MayaDevError(f"Windows setup failed: {detail}") from exc
                raise
        finally:
            # Also attempt cleanup locally in case SSH never reached the remote finally block.
            _cleanup_remote_stage(config, runner, stage)
        if (result.returncode == 0) != bool(payload.get("ok")):
            raise MayaDevError("Windows setup exit code does not match its result")
        payload["planned_actions"] = actions
        return payload


def windows_setup(config: Config, runner: Runner, *, apply: bool) -> dict[str, Any]:
    check = windows_check(config, runner)
    actions, blockers = _plan(check)
    if not apply or blockers:
        payload = _setup_payload(check, actions, blockers)
        if apply:
            payload["mode"] = "apply"
        return payload
    if not actions:
        return {
            "schema_version": 1,
            "command": "windows.setup",
            "mode": "apply",
            "ok": True,
            "changed": False,
            "changes": [],
            "check": check,
        }
    payload = _apply_setup(config, runner, actions)
    if payload.get("ok"):
        try:
            post_check = windows_check(config, runner)
        except MayaDevError as exc:
            payload["ok"] = False
            payload["verification"] = {"ok": False, "error": str(exc)}
            return payload
        payload["post_check"] = post_check
        payload["ok"] = bool(post_check.get("ok"))
        payload["verification"] = {"ok": payload["ok"]}
    return payload
