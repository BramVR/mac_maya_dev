# mac_maya_dev

Mac CLI for developing
[GG_MayaMCP](https://github.com/GimbalGoats/GG_MayaMCP) against Maya on a
Windows PC reached through Tailscale and SSH.

The Mac checkout stays authoritative. The tool sends immutable source
snapshots to Windows, then runs MCP beside Maya with stdio carried over SSH.

## Commands

- `doctor`: read-only Mac, SSH, Python, Maya, sessiond, scheduled-task, and
  loopback command-port checks.
- `windows check`: read-only Windows prerequisite and configuration report.
- `windows setup`: idempotent Windows preparation; dry-run unless `--apply` is
  passed.
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
- dedicated Windows CPython and MCP venv paths under the setup root.
- Maya 2024 and sessiond paths.
- interactive Windows account and scheduled-task name.

All global options precede the command, for example
`maya-dev --config .maya-dev.toml --json windows check`.

## One-time Windows setup

Maya launched by Windows OpenSSH appears in the wrong desktop session. Create
and update the host through the checked-in setup workflow instead of launching
Maya as an SSH child:

```sh
# Read-only readiness report.
uv run maya-dev --config .maya-dev.toml windows check

# Read-only change preview. This is the default setup mode.
uv run maya-dev --config .maya-dev.toml windows setup

# Explicit apply. Review the preview first.
uv run maya-dev --config .maya-dev.toml windows setup --apply
```

`windows setup --apply` performs only these configured changes:

- Creates the setup, deployment, deployment-staging, and sessiond state
  directories.
- Downloads the official Python 3.11.9 x64 installer from `python.org`, checks
  its pinned SHA-256, and installs it without PATH, file-association, launcher,
  or shortcut changes.
- Builds a separate MCP venv from
  [the Windows/Python 3.11 hash lock](windows/mcp-requirements-py311.lock).
- Atomically installs the existing
  [Maya 2024 sessiond launcher](windows/start-sessiond-maya2024.ps1).
- Registers `MayaDevSessiond2024` (or the configured name) for the configured
  account with `LogonType=Interactive`, no trigger, and no stored password.

The task action uses encoded PowerShell arguments and calls the repository
launcher. It is triggered only by `maya-dev start`; setup never launches Maya,
sessiond, or MCP. The configured user must be logged into the Windows desktop
for `windows check`, setup, and later interactive starts. The per-user Python
install normally needs no elevation, but creating configured root directories
or registering a task can require additional Windows rights. Setup does not
auto-elevate.

Both `windows check` and the default setup preview perform one read-only SSH
inspection and no remote writes. `--json` returns a versioned ordered check,
action, blocker, and change structure suitable for automation. A second apply
first checks state and performs no upload or write when everything matches.

## Sessiond compatibility fallback

No `gg_maya_sessiond` source, artifact, or dependency lock is available in the
Mac workspace. The example therefore requires the explicit
`sessiond.reuse_existing = true` compatibility fallback and points at the
existing Python 3.11 venv. Check/setup verify that the venv is isolated,
imports the configured module, and exposes every launcher option; setup never
installs into or modifies it. It remains externally sourced and not
reproducible.

The MCP runtime is independent: dedicated CPython plus a hash-locked venv
generated from GG_MayaMCP commit
`cf89b9907a95fade4ac49ebabc17bac1cdfda550`. Refresh the lock when upstream
runtime metadata changes:

```sh
uv pip compile ~/Projects/oss/GG_MayaMCP/pyproject.toml \
  --no-emit-package maya-mcp \
  --python-version 3.11 \
  --python-platform x86_64-pc-windows-msvc \
  --generate-hashes \
  --only-binary :all: \
  --no-annotate \
  -o windows/mcp-requirements-py311.lock
```

Then update the lock SHA-256 and GG_MayaMCP source commit in
`windows/setup-manifest.json`. Sessiond may enforce its own GG_MayaMCP
compatibility lock; a successful reuse-runtime import does not prove every new
source snapshot is compatible. `start` remains the authoritative end-to-end
check until an approved sessiond source/lock is available.

## Warm daily loop

With Maya 2024 already open on the configured loopback port, for example
`7002`:

```sh
uv run maya-dev doctor
uv run maya-dev check
uv run maya-dev deploy
uv run maya-dev connect
```

Configure the MCP client to launch `maya-dev connect`. After edits, run
`check`, `deploy`, then reconnect the client. Only MCP restarts.

Both warm `connect` and the cold sessiond lifecycle initialize GG_MayaMCP with
the configured port. Keep the port loopback-only and use `windows check` to
reject unexpected listeners before setup or startup.

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
- Setup never starts or stops processes and never changes firewall, Tailscale,
  SSH, credentials, or port configuration.

## Development

```sh
uv run ruff check .
uv run mypy src
uv run pytest
```
