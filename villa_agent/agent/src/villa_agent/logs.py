"""Structured JSON logs that never contain secrets."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_STD = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class RedactingJsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        # Longest first, so a secret that contains another is fully hidden.
        self._secrets = sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in vars(record).items():
            if k not in _STD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return self.redact(json.dumps(out, ensure_ascii=False, default=str))


def setup_logging(level: str, secrets: list[str], stream: Any = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(RedactingJsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # These libraries log raw frames at DEBUG, which would include the auth message.
    for noisy in ("websockets", "paho", "aiomqtt", "asyncio"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, logging.getLevelName(level)))
    return handler
