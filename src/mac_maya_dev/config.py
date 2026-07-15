"""Configuration loading and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .errors import MayaDevError

DEFAULT_CONFIG_NAME = ".maya-dev.toml"


@dataclass(frozen=True)
class LocalConfig:
    source: Path
    check_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class RemoteConfig:
    ssh_host: str
    deploy_root: str
    python: str
    mcp_module: str
    port: int


@dataclass(frozen=True)
class SessiondConfig:
    python: str
    state_dir: str
    maya_exe: str
    module: str
    interactive_task: str
    wait_timeout_seconds: int
    reuse_existing: bool = False


@dataclass(frozen=True)
class WindowsConfig:
    setup_root: str
    python_install_dir: str
    mcp_venv_dir: str
    launcher: str
    interactive_user: str


@dataclass(frozen=True)
class Config:
    path: Path
    local: LocalConfig
    remote: RemoteConfig
    sessiond: SessiondConfig
    windows: WindowsConfig | None = None


def find_config(explicit: Path | None, *, cwd: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise MayaDevError(f"Config not found: {path}")
        return path

    current = (cwd or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate

    user_candidate = Path.home() / ".config" / "mac_maya_dev" / "config.toml"
    if user_candidate.is_file():
        return user_candidate
    raise MayaDevError(
        f"No {DEFAULT_CONFIG_NAME} found. Copy maya-dev.toml.example and edit it."
    )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise MayaDevError(f"Missing [{name}] section")
    return value


def _text(section: dict[str, Any], key: str, section_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MayaDevError(f"[{section_name}].{key} must be a non-empty string")
    return value


def _integer(section: dict[str, Any], key: str, section_name: str) -> int:
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MayaDevError(f"[{section_name}].{key} must be an integer")
    return value


def _module(section: dict[str, Any], key: str, section_name: str) -> str:
    value = _text(section, key, section_name)
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value):
        raise MayaDevError(f"[{section_name}].{key} must be a Python module name")
    return value


def _windows_path(section: dict[str, Any], key: str, section_name: str) -> str:
    value = _text(section, key, section_name)
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        raise MayaDevError(f"[{section_name}].{key} must be an absolute Windows drive path")
    parts = value.replace("\\", "/").split("/")
    reserved_names = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    invalid_char = any(ord(char) < 32 or char in '\"*?<>|[]' for char in value)
    invalid_part = any(
        part in {".", ".."}
        or part.endswith((" ", "."))
        or (":" in part and index != 0)
        or (index != 0 and part.split(".", 1)[0].casefold() in reserved_names)
        for index, part in enumerate(parts)
    )
    if invalid_char or invalid_part:
        raise MayaDevError(f"[{section_name}].{key} contains unsafe Windows path characters")
    return value


def _same_windows_path(left: str, right: str) -> bool:
    return left.replace("\\", "/").rstrip("/").casefold() == right.replace(
        "\\", "/"
    ).rstrip("/").casefold()


def _windows_parts(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(path).parts)


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = _windows_parts(left)
    right_parts = _windows_parts(right)
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def _is_ancestor_or_same(parent: str, child: str) -> bool:
    parent_parts = _windows_parts(parent)
    child_parts = _windows_parts(child)
    return (
        len(parent_parts) <= len(child_parts)
        and child_parts[: len(parent_parts)] == parent_parts
    )


def validate_windows_layout(
    windows: WindowsConfig, remote: RemoteConfig, sessiond: SessiondConfig
) -> None:
    replaceable = {
        "[windows].python_install_dir": windows.python_install_dir,
        "[windows].mcp_venv_dir": windows.mcp_venv_dir,
        "[remote].deploy_root": remote.deploy_root,
        "[sessiond].state_dir": sessiond.state_dir,
    }
    items = list(replaceable.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if _paths_overlap(left_path, right_path):
                raise MayaDevError(
                    f"{left_name} and {right_name} must not overlap: "
                    f"{left_path!r}, {right_path!r}"
                )
    for name, path in replaceable.items():
        if _paths_overlap(windows.launcher, path):
            raise MayaDevError(
                f"[windows].launcher and {name} must not overlap: "
                f"{windows.launcher!r}, {path!r}"
            )
    replaceable_runtimes = (
        ("[windows].python_install_dir", windows.python_install_dir),
        ("[windows].mcp_venv_dir", windows.mcp_venv_dir),
    )
    external_executables = (
        ("[sessiond].python", sessiond.python),
        ("[sessiond].maya_exe", sessiond.maya_exe),
    )
    for runtime_name, runtime_path in replaceable_runtimes:
        for executable_name, executable_path in external_executables:
            if _is_ancestor_or_same(runtime_path, executable_path):
                raise MayaDevError(
                    f"{executable_name} must not be inside replaceable {runtime_name}: "
                    f"{executable_path!r}, {runtime_path!r}"
                )
    for executable_name, executable_path in external_executables:
        if _same_windows_path(windows.launcher, executable_path):
            raise MayaDevError(
                f"[windows].launcher must not replace {executable_name}: "
                f"{windows.launcher!r}"
            )
    if not _is_ancestor_or_same(windows.setup_root, windows.python_install_dir):
        raise MayaDevError("[windows].python_install_dir must be under [windows].setup_root")
    if not _is_ancestor_or_same(windows.setup_root, windows.mcp_venv_dir):
        raise MayaDevError("[windows].mcp_venv_dir must be under [windows].setup_root")
    launcher_parent = str(PureWindowsPath(windows.launcher).parent)
    if not _is_ancestor_or_same(windows.setup_root, launcher_parent):
        raise MayaDevError("[windows].launcher must be under [windows].setup_root")


def _commands(section: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    value = section.get("check_commands", [])
    if not isinstance(value, list):
        raise MayaDevError("[local].check_commands must be a list of argument lists")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise MayaDevError(
                f"[local].check_commands[{index}] must be a non-empty list of strings"
            )
        commands.append(tuple(command))
    return tuple(commands)


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MayaDevError(f"Invalid TOML in {path}: {exc}") from exc

    local_data = _section(data, "local")
    remote_data = _section(data, "remote")
    sessiond_data = _section(data, "sessiond")
    windows_value = data.get("windows")
    if windows_value is not None and not isinstance(windows_value, dict):
        raise MayaDevError("[windows] must be a table")
    windows_data = windows_value

    source_raw = _text(local_data, "source", "local")
    source = Path(source_raw).expanduser()
    if not source.is_absolute():
        source = path.parent / source
    source = source.resolve()

    port = _integer(remote_data, "port", "remote")
    if not 1 <= port <= 65535:
        raise MayaDevError("[remote].port must be between 1 and 65535")

    wait_timeout = _integer(sessiond_data, "wait_timeout_seconds", "sessiond")
    if wait_timeout < 1:
        raise MayaDevError("[sessiond].wait_timeout_seconds must be positive")

    ssh_host = _text(remote_data, "ssh_host", "remote")
    if ssh_host.startswith("-") or any(char.isspace() or ord(char) < 32 for char in ssh_host):
        raise MayaDevError("[remote].ssh_host must be a safe SSH config alias")

    remote_python = _windows_path(remote_data, "python", "remote")

    interactive_task = _text(sessiond_data, "interactive_task", "sessiond")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}", interactive_task):
        raise MayaDevError("[sessiond].interactive_task contains unsafe characters")

    reuse_value = sessiond_data.get("reuse_existing", False)
    if not isinstance(reuse_value, bool):
        raise MayaDevError("[sessiond].reuse_existing must be a boolean")
    reuse_existing = reuse_value

    windows_config = None
    if windows_data is not None:
        mcp_venv_dir = _windows_path(windows_data, "mcp_venv_dir", "windows")
        mcp_venv_root = mcp_venv_dir.rstrip("/\\")
        expected_remote_python = f"{mcp_venv_root}/Scripts/python.exe"
        if not _same_windows_path(remote_python, expected_remote_python):
            raise MayaDevError(
                "[remote].python must be [windows].mcp_venv_dir/Scripts/python.exe"
            )
        interactive_user = _text(windows_data, "interactive_user", "windows")
        if not re.fullmatch(
            r"[A-Za-z0-9_.@ -]+\\[A-Za-z0-9_.@ -]+", interactive_user
        ):
            raise MayaDevError(
                "[windows].interactive_user must be a qualified DOMAIN\\user account"
            )
        if not reuse_existing:
            raise MayaDevError(
                "[sessiond].reuse_existing must be true until a reproducible "
                "sessiond source is configured"
            )
        windows_config = WindowsConfig(
            setup_root=_windows_path(windows_data, "setup_root", "windows"),
            python_install_dir=_windows_path(
                windows_data, "python_install_dir", "windows"
            ),
            mcp_venv_dir=mcp_venv_dir,
            launcher=_windows_path(windows_data, "launcher", "windows"),
            interactive_user=interactive_user,
        )

    remote_config = RemoteConfig(
        ssh_host=ssh_host,
        deploy_root=_windows_path(remote_data, "deploy_root", "remote"),
        python=remote_python,
        mcp_module=_module(remote_data, "mcp_module", "remote"),
        port=port,
    )
    sessiond_config = SessiondConfig(
        python=_windows_path(sessiond_data, "python", "sessiond"),
        state_dir=_windows_path(sessiond_data, "state_dir", "sessiond"),
        maya_exe=_windows_path(sessiond_data, "maya_exe", "sessiond"),
        module=_module(sessiond_data, "module", "sessiond"),
        interactive_task=interactive_task,
        wait_timeout_seconds=wait_timeout,
        reuse_existing=reuse_existing,
    )
    if windows_config is not None:
        validate_windows_layout(windows_config, remote_config, sessiond_config)

    return Config(
        path=path,
        local=LocalConfig(source=source, check_commands=_commands(local_data)),
        remote=remote_config,
        sessiond=sessiond_config,
        windows=windows_config,
    )
