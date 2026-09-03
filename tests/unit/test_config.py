"""Config validation.

The theme: every test here asserts that a *bad* config is refused. Loading a
good config is one test; the other twenty are the ones that matter.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradingbot.config import Mode, Secrets, load_config
from tradingbot.errors import ConfigError, UnsafeConfigError


def test_minimal_config_loads(config_dict: dict[str, Any], write_config: Any) -> None:
    config = load_config(write_config(config_dict))
    assert config.mode is Mode.PAPER
    assert config.universe.symbols == ["SPY", "QQQ"]
    assert len(config.enabled_strategies) == 1


def test_shipped_example_config_is_valid() -> None:
    """config/example.yaml is committed as a template; it must actually load."""
    example = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
    config = load_config(example)
    assert config.mode is Mode.PAPER


# --- ADR 0008: decimals -------------------------------------------------------


def test_yaml_floats_become_exact_decimals(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config = load_config(write_config(config_dict))
    atr = config.risk.sizing.atr_multiple
    assert isinstance(atr, Decimal)
    # Decimal(2.0) would be 2.00000000000000011102230246251565404236316680908203125
    assert str(atr) == "2.0"
    assert config.risk.min_price == Decimal("5.0")


# --- no permissive defaults for money ----------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "max_daily_loss_pct",
        "max_drawdown_pct",
        "max_gross_exposure_pct",
        "max_open_positions",
        "max_position_pct",
        "min_price",
        "allow_shorts",
        "pdt_policy",
        "eod_policy",
        "kill_liquidates",
    ],
)
def test_missing_risk_limit_is_an_error(
    config_dict: dict[str, Any], write_config: Any, missing: str
) -> None:
    del config_dict["risk"][missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(write_config(config_dict))


def test_unknown_key_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    """A typo must fail, not silently leave the real limit at its default."""
    config_dict["risk"]["max_postion_pct"] = 50.0
    with pytest.raises(ConfigError, match="max_postion_pct"):
        load_config(write_config(config_dict))


# --- internal coherence -------------------------------------------------------


def test_position_larger_than_gross_exposure_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["risk"]["max_position_pct"] = 150.0
    with pytest.raises(ConfigError, match="max_position_pct"):
        load_config(write_config(config_dict))


def test_daily_loss_above_drawdown_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["risk"]["max_daily_loss_pct"] = 20.0
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        load_config(write_config(config_dict))


def test_absurd_risk_per_trade_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["risk"]["sizing"]["risk_per_trade_pct"] = 50.0
    with pytest.raises(ConfigError, match="typo"):
        load_config(write_config(config_dict))


def test_hold_policy_requires_overnight_limit(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["risk"]["eod_policy"] = "hold"
    with pytest.raises(ConfigError, match="overnight_exposure_pct"):
        load_config(write_config(config_dict))


def test_hold_policy_caps_overnight_exposure(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["risk"]["eod_policy"] = "hold"
    config_dict["risk"]["overnight_exposure_pct"] = 80.0
    with pytest.raises(ConfigError, match="50%"):
        load_config(write_config(config_dict))


def test_reconciliation_on_startup_cannot_be_disabled(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["reconciliation"] = {"on_startup": False}
    with pytest.raises(ConfigError, match="cannot be disabled"):
        load_config(write_config(config_dict))


def test_alerts_must_include_critical_events(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["observability"]["alerts"]["events"] = ["daily_summary"]
    with pytest.raises(ConfigError, match="kill_switch"):
        load_config(write_config(config_dict))


# --- Alpaca order-type constraints -------------------------------------------


def test_extended_hours_requires_limit_orders(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["session"]["trade_extended_hours"] = True
    config_dict["execution"] = {
        "default_order_type": "market",
        "use_bracket_orders": False,
    }
    with pytest.raises(ConfigError, match="limit"):
        load_config(write_config(config_dict))


def test_extended_hours_rejects_bracket_orders(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["session"]["trade_extended_hours"] = True
    config_dict["execution"] = {
        "default_order_type": "limit",
        "use_bracket_orders": True,
    }
    with pytest.raises(ConfigError, match="bracket"):
        load_config(write_config(config_dict))


def test_crypto_requires_pdt_ignore(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["account"]["asset_class"] = "crypto"
    with pytest.raises(ConfigError, match="pdt_policy"):
        load_config(write_config(config_dict))


# --- strategies ---------------------------------------------------------------


def test_strategy_symbols_must_be_in_universe(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["symbols"] = ["TSLA"]
    with pytest.raises(ConfigError, match="TSLA"):
        load_config(write_config(config_dict))


def test_duplicate_strategy_ids_are_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"].append(dict(config_dict["strategies"][0]))
    with pytest.raises(ConfigError, match="duplicate strategy id"):
        load_config(write_config(config_dict))


def test_bad_timeframe_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["timeframe"] = "5m"
    with pytest.raises(ConfigError, match="timeframe"):
        load_config(write_config(config_dict))


def test_bad_class_path_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["class"] = "tradingbot.strategies.sma.SmaCrossover"
    with pytest.raises(ConfigError, match="ClassName"):
        load_config(write_config(config_dict))


# --- ADR 0003: overrides may only tighten -------------------------------------


def test_risk_override_may_tighten(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["risk_overrides"] = {"max_position_pct": 5.0}
    config = load_config(write_config(config_dict))
    assert config.strategies[0].risk_overrides["max_position_pct"] == 5.0


def test_risk_override_may_not_loosen(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["risk_overrides"] = {"max_position_pct": 25.0}
    with pytest.raises(ConfigError, match="only tighten"):
        load_config(write_config(config_dict))


def test_risk_override_of_unknown_limit_is_rejected(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"][0]["risk_overrides"] = {"max_daily_loss_pct": 1.0}
    with pytest.raises(ConfigError, match="risk_overrides"):
        load_config(write_config(config_dict))


# --- live-mode gates ----------------------------------------------------------


def test_live_without_acknowledgement_is_refused(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["mode"] = "live"
    with pytest.raises(UnsafeConfigError, match="i-understand-the-risk"):
        load_config(write_config(config_dict, "live.yaml"))


def test_live_config_must_be_named_live(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["mode"] = "live"
    with pytest.raises(UnsafeConfigError, match="does not"):
        load_config(write_config(config_dict, "paper.yaml"), live_ack=True)


def test_live_config_accepts_a_live_filename(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["mode"] = "live"
    config = load_config(write_config(config_dict, "live.yaml"), live_ack=True)
    assert config.is_live


def test_mode_flag_cannot_override_the_file(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    with pytest.raises(UnsafeConfigError, match="contradicts"):
        load_config(write_config(config_dict), mode_override=Mode.LIVE, live_ack=True)


def test_paper_acknowledgement_flag_is_harmless(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config = load_config(write_config(config_dict), live_ack=True)
    assert config.mode is Mode.PAPER


# --- file handling ------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


# --- secrets ------------------------------------------------------------------


def test_paper_and_live_credentials_do_not_fall_back() -> None:
    secrets = Secrets(
        _env_file=None,
        alpaca_paper_key_id="PKID000000",
        alpaca_paper_secret_key="paper-secret-0123456789",
    )
    key, secret = secrets.credentials_for(Mode.PAPER)
    assert key.get_secret_value() == "PKID000000"
    assert secret.get_secret_value() == "paper-secret-0123456789"

    with pytest.raises(ConfigError, match="ALPACA_LIVE"):
        secrets.credentials_for(Mode.LIVE)


def test_live_credentials_are_not_used_for_paper() -> None:
    secrets = Secrets(
        _env_file=None,
        alpaca_live_key_id="AKID000000",
        alpaca_live_secret_key="live-secret-0123456789",
    )
    with pytest.raises(ConfigError, match="ALPACA_PAPER"):
        secrets.credentials_for(Mode.PAPER)
