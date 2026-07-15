"""Safe SSH and PowerShell command construction."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from .errors import MayaDevError


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


def powershell_literal(value: str) -> str:
    """Return a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class Runner:
    """Subprocess boundary, replaceable in tests."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        capture: bool = True,
        stdin: TextIO | None = None,
    ) -> Result:
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                check=False,
                text=True,
                stdin=stdin,
                capture_output=capture,
            )
        except OSError as exc:
            return Result(127, "", str(exc))
        return Result(
            completed.returncode,
            completed.stdout if capture else "",
            completed.stderr if capture else "",
        )

    def exec(self, args: Sequence[str]) -> int:
        try:
            completed = subprocess.run(list(args), check=False)
        except OSError as exc:
            raise MayaDevError(f"Cannot start {args[0]}: {exc}") from exc
        return completed.returncode


def ssh_args(host: str, script: str, *, tty: bool = False) -> list[str]:
    mode = "-tt" if tty else "-T"
    return [
        "ssh",
        mode,
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        host,
        "powershell",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded_powershell(script),
    ]


def run_powershell(runner: Runner, host: str, script: str) -> Result:
    return runner.run(ssh_args(host, script))


def run_powershell_with_stdin(
    runner: Runner, host: str, script: str, stdin_text: str
) -> Result:
    """Run a small encoded bootstrap with a larger data payload on stdin."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as handle:
        handle.write(stdin_text)
        handle.seek(0)
        return runner.run(ssh_args(host, script), stdin=handle)


def parse_last_json_object(output: str, *, action: str) -> dict[str, Any]:
    """Parse the final JSON document after any preceding structured logs."""
    for index in range(len(output) - 1, -1, -1):
        if output[index] != "{":
            continue
        try:
            payload = json.loads(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
        raise MayaDevError(f"{action} returned an unexpected JSON value")
    raise MayaDevError(f"{action} returned invalid JSON: {output.strip()}")


def parse_json_result(result: Result, *, action: str) -> dict[str, Any]:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise MayaDevError(f"{action} failed: {detail}")
    return parse_last_json_object(result.stdout, action=action)


def emit(payload: Any, *, json_output: bool, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    if json_output:
        print(json.dumps(payload, indent=2, default=str), file=output)
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, default=str)
            else:
                rendered = str(value)
            print(f"{key}: {rendered}", file=output)
        return
    print(payload, file=output)
