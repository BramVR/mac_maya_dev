from __future__ import annotations

from pathlib import Path

import pytest

from mac_maya_dev.config import find_config, load_config
from mac_maya_dev.errors import MayaDevError


def write_config(path: Path, *, port: int = 7001) -> None:
    path.write_text(
        f"""
[local]
source = "source"
check_commands = [["uv", "run", "pytest"]]
[remote]
ssh_host = "maya-win"
deploy_root = "C:/deploy"
python = "C:/Python/python.exe"
mcp_module = "maya_mcp.server"
port = {port}
[sessiond]
python = "C:/sessiond/python.exe"
state_dir = "C:/state"
maya_exe = "C:/Maya/maya.exe"
module = "gg_maya_sessiond.cli"
interactive_task = "MayaDevSessiond2024"
wait_timeout_seconds = 30
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_find_and_load_config_resolves_source_relative_to_config(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    config_path = root / ".maya-dev.toml"
    write_config(config_path)

    found = find_config(None, cwd=nested)
    config = load_config(found)

    assert found == config_path
    assert config.local.source == root / "source"
    assert config.local.check_commands == (("uv", "run", "pytest"),)
    assert config.remote.port == 7001


def test_explicit_missing_config_fails(tmp_path: Path) -> None:
    with pytest.raises(MayaDevError, match="Config not found"):
        find_config(tmp_path / "missing.toml")


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_fails(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    write_config(path, port=port)
    with pytest.raises(MayaDevError, match="between 1 and 65535"):
        load_config(path)


def test_missing_section_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[local]\nsource='.'\n", encoding="utf-8")
    with pytest.raises(MayaDevError, match=r"Missing \[remote\]"):
        load_config(path)
