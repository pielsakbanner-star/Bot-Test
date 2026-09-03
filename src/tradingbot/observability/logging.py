"""Structured logging (ADR 0007).

Two responsibilities beyond "write log lines":

* **Correlation.** A single id is bound once, when a bar is sealed, and every
  line emitted downstream carries it -- signal, risk decision, order, fill.
  That is requirement N-5, and it is the difference between "the bot bought
  something" and "this bar, through this strategy, produced this order".
* **Redaction.** Secrets are scrubbed structurally, in a processor, rather than
  by asking every call site to remember. Both by field name and by value, so a
  key that ends up interpolated into a URL or an exception message is caught
  too (requirement N-6).
"""

from __future__ import annotations

import hashlib
import logging
import logging.handlers
import sys
import uuid
from collections.abc import Iterable, Iterator, MutableMapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import structlog
from pydantic import SecretStr

from tradingbot.config import LogFormat, Mode, ObservabilityConfig

REDACTED: Final = "***REDACTED***"

# Field names whose values are always redacted, matched case-insensitively as
# substrings so `alpaca_secret_key` and `Authorization` are both covered.
_SENSITIVE_FIELDS: Final = ("key", "secret", "token", "password", "authorization")

# Field names that contain a sensitive substring but are not themselves secret.
_FIELD_ALLOWLIST: Final = frozenset(
    {"client_order_id", "idempotency_key", "key_id_present", "public_key_id"}
)

# Exact secret values registered at startup, redacted wherever they appear.
_KNOWN_SECRETS: set[str] = set()

_MIN_SECRET_LEN: Final = 8


def register_secret(value: str | SecretStr | None) -> None:
    """Register a literal secret so it is redacted wherever it appears.

    Short or empty values are ignored: redacting a two-character string would
    mangle unrelated log output for no benefit.
    """
    if value is None:
        return
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if len(raw) >= _MIN_SECRET_LEN:
        _KNOWN_SECRETS.add(raw)


def reset_secrets() -> None:
    """Clear registered secrets. For tests."""
    _KNOWN_SECRETS.clear()


def _is_sensitive(field: str) -> bool:
    lowered = field.lower()
    if lowered in _FIELD_ALLOWLIST:
        return False
    return any(marker in lowered for marker in _SENSITIVE_FIELDS)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        for secret in _KNOWN_SECRETS:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive(str(k)) else _scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub_value(v) for v in value]
        return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def scrub_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact secrets by field name and by value."""
    for field in list(event_dict):
        if _is_sensitive(field):
            event_dict[field] = REDACTED
        else:
            event_dict[field] = _scrub_value(event_dict[field])
    return event_dict


def _mode_processor(mode: Mode) -> Any:
    """Stamp every line with PAPER or LIVE. Cheap, and prevents a whole class
    of "which account was this?" confusion when reading logs after the fact."""
    tag = mode.value.upper()

    def processor(
        _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict["mode"] = tag
        return event_dict

    return processor


# -----------------------------------------------------------------------------
# Correlation ids
# -----------------------------------------------------------------------------


def new_correlation_id() -> str:
    """A fresh id, for work not triggered by a bar (startup, reconciliation)."""
    return uuid.uuid4().hex[:12]


def correlation_id_for_bar(symbol: str, timeframe: str, timestamp: datetime) -> str:
    """A deterministic id for a bar.

    Deterministic so that replaying a recorded session produces the same ids as
    the original run, which is what makes `tradingbot replay` diffable against
    the live journal.
    """
    seed = f"{symbol}|{timeframe}|{timestamp.isoformat()}".encode()
    return hashlib.blake2s(seed, digest_size=6).hexdigest()


@contextmanager
def correlation_scope(correlation_id: str, **extra: Any) -> Iterator[str]:
    """Bind a correlation id (and any extra context) for the duration of a block."""
    tokens = structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id, **extra
    )
    try:
        yield correlation_id
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def configure_logging(
    config: ObservabilityConfig,
    *,
    mode: Mode,
    secrets: Iterable[str | SecretStr | None] = (),
    quiet_console: bool = False,
) -> None:
    """Configure structlog and the stdlib root logger.

    Console output always goes to stderr so that stdout stays free for machine
    readable command output. A rotating file handler is added when ``log_dir``
    is set.

    ``quiet_console`` raises the console handler to WARNING while the log file
    keeps the configured level. One-shot commands use it: the result of
    `doctor` is its table, and interleaving JSON log lines with that table
    helps nobody. Warnings and errors still reach the terminal.
    """
    for secret in secrets:
        register_secret(secret)

    level = getattr(logging, config.log_level)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _mode_processor(mode),
        scrub_processor,
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Any
    if config.log_format is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        # Close as well as remove: reconfiguring without this leaks the open
        # log file, which matters for a process meant to run all day.
        handler.close()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    if quiet_console:
        stream.setLevel(max(level, logging.WARNING))
    root.addHandler(stream)

    if config.log_dir is not None:
        log_dir = Path(config.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.TimedRotatingFileHandler(
            log_dir / f"tradingbot-{mode.value}.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
            utc=True,
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    root.setLevel(level)
    # The SDK's HTTP client is chatty at DEBUG and can log request URLs.
    for noisy in ("urllib3", "httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Use the module name."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
