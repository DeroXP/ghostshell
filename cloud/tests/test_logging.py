"""Log formatting guards: nothing that reaches a log call can forge a line."""

from __future__ import annotations

import json
import logging
import sys

from logconfig import JsonFormatter, TextFormatter


def _record(msg: str, *args, level=logging.INFO, exc_info=None) -> logging.LogRecord:
    r = logging.LogRecord("t", level, "", 0, msg, args, exc_info)
    r.request_id = "rid"
    return r


def test_text_formatter_escapes_control_characters():
    f = TextFormatter("%(levelname)s [%(request_id)s] %(message)s")
    out = f.format(_record("device %s", "evil\nINFO forged line\r\x1b[31m"))
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert "forged line" in out  # still readable, just inert
    assert out.startswith("INFO [rid] device evil\\x0aINFO forged line")


def test_text_formatter_keeps_traceback_newlines():
    f = TextFormatter("%(message)s")
    try:
        raise ValueError("boom")
    except ValueError:
        out = f.format(_record("failed", level=logging.ERROR, exc_info=sys.exc_info()))
    assert out.startswith("failed\nTraceback")
    assert "ValueError: boom" in out


def test_json_formatter_is_one_line_per_record():
    out = JsonFormatter().format(_record("a\nb %s", "c\rd"))
    assert "\n" not in out and "\r" not in out
    parsed = json.loads(out)
    assert parsed["message"] == "a\nb c\rd"
    assert parsed["request_id"] == "rid"
    assert parsed["level"] == "INFO"
