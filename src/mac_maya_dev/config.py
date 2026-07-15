"""Configuration loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class Config:
    path: Path
    local: LocalConfig
    remote: RemoteConfig
    sessiond: SessiondConfig


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

    return Config(
        path=path,
        local=LocalConfig(source=source, check_commands=_commands(local_data)),
        remote=RemoteConfig(
            ssh_host=_text(remote_data, "ssh_host", "remote"),
            deploy_root=_text(remote_data, "deploy_root", "remote"),
            python=_text(remote_data, "python", "remote"),
            mcp_module=_text(remote_data, "mcp_module", "remote"),
            port=port,
        ),
        sessiond=SessiondConfig(
            python=_text(sessiond_data, "python", "sessiond"),
            state_dir=_text(sessiond_data, "state_dir", "sessiond"),
            maya_exe=_text(sessiond_data, "maya_exe", "sessiond"),
            module=_text(sessiond_data, "module", "sessiond"),
            interactive_task=_text(sessiond_data, "interactive_task", "sessiond"),
            wait_timeout_seconds=wait_timeout,
        ),
    )
