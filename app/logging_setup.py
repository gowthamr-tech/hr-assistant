"""Structured JSON logging with a personal-data scrubbing filter.

Log call sites only pass IDs, counts and statuses. The filter is a second line
of defence: it replaces any known employee name with the employee ID, masks
emails and removes phone numbers from every record before it is written.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.security.pii import scrub_all

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class PiiScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Scrub the template and each argument separately: some formatters (uvicorn's access
            # log) need the original argument tuple.
            if isinstance(record.msg, str):
                record.msg = scrub_all(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(scrub_all(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: scrub_all(v) if isinstance(v, str) else v for k, v in record.args.items()}
            for key, value in list(record.__dict__.items()):
                if key not in _RESERVED and isinstance(value, str):
                    setattr(record, key, scrub_all(value))
        except Exception:  # never let logging break a request
            record.msg, record.args = "[log message dropped by scrubber]", ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            # Exception type only; messages and tracebacks can contain user text.
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


class PrettyFormatter(logging.Formatter):
    """One readable line per record for local terminals: time, level, logger, message, fields."""

    _COLORS = {"DEBUG": "\033[2m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m",
               "CRITICAL": "\033[1;31m"}
    _DIM, _BOLD, _RESET = "\033[2m", "\033[1m", "\033[0m"

    def __init__(self, color: bool = True) -> None:
        super().__init__()
        self.color = color

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{self._RESET}" if self.color else text

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        level = self._c(self._COLORS.get(record.levelname, ""), f"{record.levelname:<7}")
        logger = record.name.rsplit(".", 1)[-1][:14]
        fields = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        rid = fields.pop("request_id", None)
        fields.pop("event", None)  # duplicates msg
        parts = [self._c(self._DIM, ts), level, self._c(self._DIM, f"{logger:<14}")]
        if rid:
            parts.append(self._c(self._DIM, f"[{str(rid)[:8]}]"))
        parts.append(self._c(self._BOLD, record.getMessage()))
        for k, v in fields.items():
            if isinstance(v, (list, tuple)):
                v = ",".join(map(str, v)) or "-"
            parts.append(f"{self._c(self._DIM, k + '=')}{v}")
        if record.exc_info and record.exc_info[0] is not None:
            parts.append(f"{self._c(self._DIM, 'exc_type=')}{record.exc_info[0].__name__}")
        return " ".join(parts)


def setup_logging(level: str = "INFO", log_file: str = "", fmt: str = "auto") -> None:
    """fmt: "json", "pretty", or "auto" (pretty when stdout is a terminal). Log files are always JSON."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    tty = sys.stdout.isatty()
    pretty = fmt == "pretty" or (fmt == "auto" and tty)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(PrettyFormatter(color=tty) if pretty else JsonFormatter())
    handlers: list[logging.Handler] = [console]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)
    for h in handlers:
        h.addFilter(PiiScrubFilter())
        root.addHandler(h)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(PiiScrubFilter())
    # Third-party request logs can include URLs and bodies; keep them quiet.
    for noisy in ("httpx", "httpcore", "anthropic", "openai", "google", "google_genai", "urllib3", "fastembed"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
