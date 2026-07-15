# mac_maya_dev

Mac CLI for developing
[GG_MayaMCP](https://github.com/GimbalGoats/GG_MayaMCP) against Maya on a
Windows PC reached through Tailscale and SSH.

The Mac checkout stays authoritative. The tool sends immutable source
snapshots to Windows, then runs MCP beside Maya with stdio carried over SSH.

## Commands

- `doctor`: read-only Mac, SSH, Python, Maya, sessiond, scheduled-task, and
  loopback command-port checks.
- `check`: configured Mac lint, type, and test commands.
- `deploy`: Git-aware, content-addressed source upload.
- `connect`: warm remote MCP connection; Maya stays open.
- `start`, `status`, `call`, `stop`, `restart`: cold sessiond lifecycle.

Direct MCP and sessiond are exclusive modes. A shared Windows lifetime lock
and session ownership checks prevent two MCP clients using one Maya port.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/BramVR/mac_maya_dev.git ~/Projects/mac_maya_dev
cd ~/Projects/mac_maya_dev
uv sync
cp maya-dev.toml.example .maya-dev.toml
```

Edit `.maya-dev.toml`. Important values:

- Mac GG_MayaMCP checkout.
- SSH alias from `~/.ssh/config`.
- standalone Windows Python 3.11 or 3.12.
- Maya 2024 and sessiond paths.
- interactive scheduled-task name.

Windows Python must already contain GG_MayaMCP dependencies. This tool does
not install packages or change host configuration.

## One-time Windows task

Maya launched by Windows OpenSSH appears in the wrong desktop session. Create
one scheduled task such as `MayaDevSessiond2024`:

- Select **Run only when user is logged on**.
- Run `powershell.exe` with
  [windows/start-sessiond-maya2024.ps1](windows/start-sessiond-maya2024.ps1).
- Pass `CurrentFile`, `SessiondPython`, `StateDir`, `MayaExe`, and `McpPython`.
- Store no credentials in task arguments.

Example arguments:

```text
-NoProfile -File C:\maya-dev\start-sessiond-maya2024.ps1 -CurrentFile C:\maya-mcp-dev\current.json -SessiondPython C:\maya-stall\sessiond-venv311\Scripts\python.exe -StateDir C:\maya-stall\sessiond-maya2024 -MayaExe "C:\Program Files\Autodesk\Maya2024\bin\maya.exe" -McpPython C:\maya-dev\python311\python.exe
```

Copy the launcher once during host setup. `maya-dev` checks and triggers the
task; it never creates or edits scheduled tasks.

## Warm daily loop

With Maya 2024 already open on localhost port `7001`:

```sh
uv run maya-dev doctor
uv run maya-dev check
uv run maya-dev deploy
uv run maya-dev connect
```

Configure the MCP client to launch `maya-dev connect`. After edits, run
`check`, `deploy`, then reconnect the client. Only MCP restarts.

Direct GG_MayaMCP currently uses port `7001`; `connect` rejects another port.

## Cold sessiond loop

For Maya startup, compatibility-bootstrap, or recovery changes:

```sh
uv run maya-dev deploy
uv run maya-dev start
uv run maya-dev call scene.info
uv run maya-dev status
```

After another edit: `check`, `deploy`, then `restart`. Current sessiond has no
public MCP-only reload, so `restart` also restarts Maya through the interactive
task.

```sh
uv run maya-dev call --list
uv run maya-dev call nodes.list type=transform
uv run maya-dev call tool.name --input-json '{"key":"value"}'
```

## Deployment and safety

Snapshots include tracked and non-ignored untracked files. Deployments live at
`C:/maya-mcp-dev/deployments/<content-hash>`; `current.json` selects one.
Existing deployments remain immutable. Old deployments are not pruned yet.

- Blocks symlinks, unsafe Windows names, path collisions, `.env` files, and
  common private-key formats.
- Keeps Maya commandPort on loopback; never exposes it through Tailscale.
- Uses encoded PowerShell, SSH batch mode, and keepalives.
- Redacts sessiond call tokens.
- Never force-stops processes or changes firewall, Tailscale, SSH, or tasks.

## Development

```sh
uv run ruff check .
uv run mypy src
uv run pytest
```
