"""Logging setup.

Two formats:
  • JSON lines (default on Railway / in production).  Railway's log explorer
    indexes the top-level keys, so you can filter on `level`, `logger` or
    `request_id` instead of grepping text.
  • Plain text (default locally) for readability in a terminal.

Every record carries the id of the request being served (from the
RequestIDMiddleware's contextvar), so a 500 in the logs can be matched to the
`X-Request-ID` header the client received.  The PrivacyFilter from security.py
is attached to the handler, so client IPs are scrubbed from *every* logger —
uvicorn's access log, asyncpg, anything a dependency emits — not just ours.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
import time

import orjson

from security import PrivacyFilter

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Loggers uvicorn configures with its own handlers before importing the app.
# Routing them through the root handler gives them our format, the request id
# and the privacy filter.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class RequestIdFilter(logging.Filter):
    """Stamps `record.request_id` from the contextvar unless the caller already
    supplied one via `extra={"request_id": ...}`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.gmtime(record.created)
        payload: dict[str, object] = {
            "ts": f"{time.strftime('%Y-%m-%dT%H:%M:%S', ts)}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(payload).decode()


_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class TextFormatter(logging.Formatter):
    """Plain-text lines for local development.

    Control characters in the rendered message are escaped (`\\x0a` for a
    newline, and so on), so a value that reaches a log call unsanitised can
    never forge a second log line.  Exception tracebacks are appended by the
    base class after this step and keep their newlines.  JSON output needs
    no such guard: orjson escapes control characters itself.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        record.message = _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", record.message)
        return super().formatMessage(record)


def configure_logging(level: str, *, json_logs: bool) -> None:
    """Install the single root handler everything logs through.  Idempotent."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter(_TEXT_FORMAT))
    handler.addFilter(RequestIdFilter())
    handler.addFilter(PrivacyFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
