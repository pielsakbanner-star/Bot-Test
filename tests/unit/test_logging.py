"""Logging: redaction and correlation.

Redaction is a safety control (requirement N-6), so it is tested like one.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from tradingbot.config import LogFormat, Mode, ObservabilityConfig
from tradingbot.observability.logging import (
    REDACTED,
    configure_logging,
    correlation_id_for_bar,
    correlation_scope,
    get_logger,
    new_correlation_id,
    register_secret,
    reset_secrets,
    scrub_processor,
)


@pytest.fixture(autouse=True)
def _clean_secrets() -> Any:
    reset_secrets()
    yield
    reset_secrets()
    structlog.reset_defaults()


def scrub(event: dict[str, Any]) -> dict[str, Any]:
    return dict(scrub_processor(None, "info", event))


# --- redaction by field name --------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "ALPACA_SECRET_KEY",
        "secret",
        "access_token",
        "password",
        "Authorization",
    ],
)
def test_sensitive_field_names_are_redacted(field: str) -> None:
    assert scrub({field: "hunter2-hunter2"})[field] == REDACTED


@pytest.mark.parametrize("field", ["client_order_id", "idempotency_key"])
def test_allowlisted_fields_survive(field: str) -> None:
    """client_order_id contains 'key'/'id' but is not a secret -- and we need to
    read it to trace an order."""
    assert scrub({field: "sma-SPY-1234-abcd"})[field] == "sma-SPY-1234-abcd"


def test_nested_sensitive_fields_are_redacted() -> None:
    out = scrub({"payload": {"api_key": "abcdefghijkl", "symbol": "SPY"}})
    assert out["payload"]["api_key"] == REDACTED
    assert out["payload"]["symbol"] == "SPY"


def test_secretstr_is_redacted_anywhere() -> None:
    assert scrub({"anything": SecretStr("abcdefghijkl")})["anything"] == REDACTED


# --- redaction by value -------------------------------------------------------


def test_registered_secret_is_redacted_inside_free_text() -> None:
    register_secret("PKSUPERSECRETKEY123")
    out = scrub({"event": "GET https://api/x?key=PKSUPERSECRETKEY123 failed"})
    assert "PKSUPERSECRETKEY123" not in out["event"]
    assert REDACTED in out["event"]


def test_registered_secret_is_redacted_in_nested_lists() -> None:
    register_secret("PKSUPERSECRETKEY123")
    out = scrub({"args": ["--key", "PKSUPERSECRETKEY123"]})
    assert out["args"] == ["--key", REDACTED]


def test_short_values_are_not_registered() -> None:
    """Redacting a short string would mangle unrelated output."""
    register_secret("abc")
    assert scrub({"event": "abc happened"})["event"] == "abc happened"


def test_secretstr_can_be_registered() -> None:
    register_secret(SecretStr("PKSUPERSECRETKEY123"))
    out = scrub({"event": "used PKSUPERSECRETKEY123"})
    assert "PKSUPERSECRETKEY123" not in out["event"]


# --- correlation ids ----------------------------------------------------------


def test_bar_correlation_id_is_deterministic() -> None:
    ts = datetime(2026, 9, 3, 14, 35, tzinfo=UTC)
    first = correlation_id_for_bar("SPY", "5Min", ts)
    assert first == correlation_id_for_bar("SPY", "5Min", ts)
    assert first != correlation_id_for_bar("QQQ", "5Min", ts)


def test_new_correlation_ids_differ() -> None:
    assert new_correlation_id() != new_correlation_id()


# --- end to end ---------------------------------------------------------------


def _emit(tmp_path: Any, level: str = "INFO") -> list[dict[str, Any]]:
    config = ObservabilityConfig(
        log_level=level, log_format=LogFormat.JSON, log_dir=tmp_path / "logs"
    )
    configure_logging(config, mode=Mode.PAPER, secrets=["PKSUPERSECRETKEY123"])
    log = get_logger("test")
    with correlation_scope(correlation_id_for_bar("SPY", "5Min", datetime.now(UTC))):
        log.info("order.submitted", symbol="SPY", api_key="PKSUPERSECRETKEY123")
    logging.getLogger().handlers[-1].flush()
    log_file = next((tmp_path / "logs").glob("*.log"))
    return [
        json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
    ]


def test_emitted_line_carries_correlation_id_and_mode(tmp_path: Any) -> None:
    (entry,) = _emit(tmp_path)
    assert entry["event"] == "order.submitted"
    assert entry["symbol"] == "SPY"
    assert entry["mode"] == "PAPER"
    assert len(entry["correlation_id"]) == 12
    assert "timestamp" in entry


def test_emitted_line_redacts_the_key(tmp_path: Any) -> None:
    (entry,) = _emit(tmp_path)
    assert entry["api_key"] == REDACTED
    assert "PKSUPERSECRETKEY123" not in json.dumps(entry)


def test_correlation_id_is_unbound_after_the_scope(tmp_path: Any) -> None:
    config = ObservabilityConfig(log_format=LogFormat.JSON, log_dir=tmp_path / "logs")
    configure_logging(config, mode=Mode.PAPER)
    log = get_logger("test")
    with correlation_scope("abc123abc123"):
        log.info("inside")
    log.info("outside")
    logging.getLogger().handlers[-1].flush()
    log_file = next((tmp_path / "logs").glob("*.log"))
    entries = [
        json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
    ]
    assert entries[0]["correlation_id"] == "abc123abc123"
    assert "correlation_id" not in entries[1]
