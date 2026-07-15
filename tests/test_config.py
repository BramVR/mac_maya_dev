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
python = "C:/maya-dev/mcp-venv311/Scripts/python.exe"
mcp_module = "maya_mcp.server"
port = {port}
[sessiond]
python = "C:/sessiond/python.exe"
state_dir = "C:/state"
maya_exe = "C:/Maya/maya.exe"
module = "gg_maya_sessiond.cli"
interactive_task = "MayaDevSessiond2024"
wait_timeout_seconds = 30
reuse_existing = true
[windows]
setup_root = "C:/maya-dev"
python_install_dir = "C:/maya-dev/cpython-3.11.9"
mcp_venv_dir = "C:/maya-dev/mcp-venv311"
launcher = "C:/maya-dev/start-sessiond-maya2024.ps1"
interactive_user = "DESKTOP-NAME\\\\maya-user"
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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('ssh_host = "maya-win"', 'ssh_host = "-oProxyCommand=bad"', "safe SSH"),
        ('mcp_module = "maya_mcp.server"', 'mcp_module = "maya_mcp;bad"', "module name"),
        (
            'launcher = "C:/maya-dev/start-sessiond-maya2024.ps1"',
            'launcher = "C:/maya-dev/bad\\\";Write-Host owned"',
            "unsafe Windows path",
        ),
        (
            'interactive_task = "MayaDevSessiond2024"',
            'interactive_task = "bad;task"',
            "unsafe characters",
        ),
    ],
)
def test_config_rejects_command_and_path_injection(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(MayaDevError, match=message):
        load_config(path)


def test_config_requires_explicit_sessiond_reuse(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("reuse_existing = true", "reuse_existing = false"),
        encoding="utf-8",
    )
    with pytest.raises(MayaDevError, match="reproducible sessiond source"):
        load_config(path)


def test_legacy_config_without_windows_section_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8")
    text = text.replace("reuse_existing = true\n", "")
    text = text[: text.index("[windows]")]
    path.write_text(text, encoding="utf-8")

    config = load_config(path)

    assert config.windows is None
    assert config.sessiond.reuse_existing is False


def test_config_rejects_replaceable_directory_overlap(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'python = "C:/maya-dev/mcp-venv311/Scripts/python.exe"',
        'python = "C:/deploy/Scripts/python.exe"',
    ).replace('mcp_venv_dir = "C:/maya-dev/mcp-venv311"', 'mcp_venv_dir = "C:/deploy"')
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="must not overlap"):
        load_config(path)


@pytest.mark.parametrize("suffix", [".", " "])
def test_config_rejects_win32_trailing_aliases(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'python_install_dir = "C:/maya-dev/cpython-3.11.9"',
        f'python_install_dir = "C:/maya-dev/cpython-3.11.9{suffix}"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="unsafe Windows path"):
        load_config(path)


def test_config_rejects_win32_reserved_device_names(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'launcher = "C:/maya-dev/start-sessiond-maya2024.ps1"',
        'launcher = "C:/maya-dev/CON.txt"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="unsafe Windows path"):
        load_config(path)


def test_config_rejects_sessiond_runtime_inside_replaceable_mcp_venv(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'python = "C:/sessiond/python.exe"',
        'python = "C:/maya-dev/mcp-venv311/Scripts/python.exe"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="must not be inside replaceable"):
        load_config(path)


def test_config_rejects_maya_inside_replaceable_mcp_venv(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'maya_exe = "C:/Maya/maya.exe"',
        'maya_exe = "C:/maya-dev/mcp-venv311/maya.exe"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="must not be inside replaceable"):
        load_config(path)


def test_config_rejects_launcher_replacing_external_executable(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'launcher = "C:/maya-dev/start-sessiond-maya2024.ps1"',
        'launcher = "C:/sessiond/python.exe"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match="must not replace"):
        load_config(path)


def test_config_requires_qualified_interactive_user(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        'interactive_user = "DESKTOP-NAME\\\\maya-user"',
        'interactive_user = "maya-user"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(MayaDevError, match=r"qualified DOMAIN\\user"):
        load_config(path)
