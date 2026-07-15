# Changelog

## Unreleased

- Fix live Windows setup path handling, package-inventory verification, staging, and empty scheduled-task trigger detection.
- Add read-only Windows host checks and explicit dry-run/apply setup for pinned CPython, hash-locked MCP dependencies, the Maya 2024 launcher, and a passwordless interactive scheduled task.
- Support the configured loopback Maya commandPort in both warm direct MCP and cold sessiond workflows.
- Initial Mac-to-Windows GG_MayaMCP development CLI with diagnostics, checks, content-addressed deployment, SSH stdio, and sessiond lifecycle commands.
