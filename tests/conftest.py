from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac_maya_dev.config import Config, LocalConfig, RemoteConfig, SessiondConfig


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "GG_MayaMCP"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True
    )
    (source / "pyproject.toml").write_text("[project]\nname='maya-mcp'\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    return source


@pytest.fixture
def config(source_repo: Path, tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / ".maya-dev.toml",
        local=LocalConfig(
            source=source_repo,
            check_commands=(("uv", "run", "ruff", "check", "."), ("uv", "run", "pytest")),
        ),
        remote=RemoteConfig(
            ssh_host="maya-win",
            deploy_root="C:/maya-mcp-dev",
            python="C:/Python311/python.exe",
            mcp_module="maya_mcp.server",
            port=7001,
        ),
        sessiond=SessiondConfig(
            python="C:/sessiond/python.exe",
            state_dir="C:/maya-stall/sessiond-maya2024",
            maya_exe="C:/Program Files/Autodesk/Maya2024/bin/maya.exe",
            module="gg_maya_sessiond.cli",
            interactive_task="MayaDevSessiond2024",
            wait_timeout_seconds=180,
        ),
    )
