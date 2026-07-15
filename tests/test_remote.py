from __future__ import annotations

import base64
import io
import sys

import pytest

from mac_maya_dev.errors import MayaDevError
from mac_maya_dev.remote import (
    Result,
    Runner,
    emit,
    encoded_powershell,
    parse_json_result,
    parse_last_json_object,
    powershell_literal,
)


def test_powershell_literal_escapes_single_quotes() -> None:
    assert powershell_literal("C:/it's/here") == "'C:/it''s/here'"


def test_encoded_powershell_round_trips_unicode() -> None:
    script = "Write-Output 'Maya 🐐'"
    decoded = base64.b64decode(encoded_powershell(script)).decode("utf-16-le")
    assert decoded == script


def test_parse_json_result() -> None:
    assert parse_json_result(Result(0, '{"ok":true}', ""), action="test") == {"ok": True}


def test_parse_last_json_object_skips_preceding_logs() -> None:
    output = '{"level":"info","message":"starting"}\n{\n  "ok": true,\n  "value": 3\n}\n'
    assert parse_last_json_object(output, action="test") == {"ok": True, "value": 3}


def test_parse_json_result_reports_remote_error() -> None:
    with pytest.raises(MayaDevError, match="test failed: denied"):
        parse_json_result(Result(1, "", "denied\n"), action="test")


def test_parse_json_result_rejects_non_object() -> None:
    with pytest.raises(MayaDevError, match="invalid JSON"):
        parse_json_result(Result(0, "[]", ""), action="test")


def test_runner_process_boundaries() -> None:
    runner = Runner()
    result = runner.run([sys.executable, "-c", "print('ok')"])
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert runner.exec([sys.executable, "-c", "pass"]) == 0


def test_runner_reports_missing_executable() -> None:
    runner = Runner()
    result = runner.run(["/definitely/missing/maya-dev-test"])
    assert result.returncode == 127
    with pytest.raises(MayaDevError, match="Cannot start"):
        runner.exec(["/definitely/missing/maya-dev-test"])


def test_emit_human_and_json() -> None:
    human = io.StringIO()
    emit({"ok": True, "items": [1, 2]}, json_output=False, stream=human)
    assert "ok: True" in human.getvalue()
    assert "items: [1, 2]" in human.getvalue()

    machine = io.StringIO()
    emit({"ok": True}, json_output=True, stream=machine)
    assert '"ok": true' in machine.getvalue()
