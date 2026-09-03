"""Typed configuration loading and validation.

Three layers, merged in order: built-in defaults, the YAML file, then the
environment (secrets and host-specific overrides only). See
docs/configuration.md.

Two properties this module is responsible for:

* **Nothing that affects money is silently defaulted.** Every field on
  :class:`RiskConfig` is required. A missing limit is a startup error, not a
  fallback to something permissive.
* **Unknown keys are errors.** Every model sets ``extra="forbid"``, so a
  mistyped ``max_postion_pct`` fails loudly instead of leaving the real limit
  at its default.

Parsing here is side-effect free -- it does not touch the network and does not
import strategy code. The checks that need those live in :mod:`tradingbot.doctor`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradingbot.errors import ConfigError, UnsafeConfigError

# -----------------------------------------------------------------------------
# Numeric types (ADR 0008)
# -----------------------------------------------------------------------------


def _decimal_via_str(value: Any) -> Any:
    """Route floats through ``str`` so YAML ``2.0`` never becomes a binary float.

    ``Decimal(2.0)`` is 2.00000000000000011102230246251565404236316680908203125.
    ``Decimal("2.0")`` is 2.0. Config files are written by humans in decimal, so
    the string path is the one that preserves what they meant.
    """
    if isinstance(value, float):
        return str(value)
    return value


Money = Annotated[Decimal, BeforeValidator(_decimal_via_str)]
"""A monetary amount. Always exact; never constructed from a float."""

Percent = Annotated[Decimal, BeforeValidator(_decimal_via_str), Field(ge=0, le=100)]
"""A percentage in the range 0-100 inclusive."""

PositivePercent = Annotated[
    Decimal, BeforeValidator(_decimal_via_str), Field(gt=0, le=100)
]


# -----------------------------------------------------------------------------
# Enumerations
# -----------------------------------------------------------------------------


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class DataFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"


class AssetClass(StrEnum):
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"


class SizingMethod(StrEnum):
    VOLATILITY_TARGET = "volatility_target"
    FIXED_FRACTIONAL = "fixed_fractional"
    FIXED_NOTIONAL = "fixed_notional"
    EQUAL_WEIGHT = "equal_weight"


class PdtPolicy(StrEnum):
    STRICT = "strict"
    BLOCK_ENTRIES = "block_entries"
    IGNORE = "ignore"


class EodPolicy(StrEnum):
    FLATTEN_ALL = "flatten_all"
    FLATTEN_INTRADAY = "flatten_intraday"
    HOLD = "hold"


class DisconnectPolicy(StrEnum):
    HALT = "halt"
    FLATTEN = "flatten"


class OrphanPositionPolicy(StrEnum):
    ADOPT = "adopt"
    FLATTEN = "flatten"
    IGNORE = "ignore"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class AlertEvent(StrEnum):
    KILL_SWITCH = "kill_switch"
    RISK_HALT = "risk_halt"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    ORDER_REJECT_STREAK = "order_reject_streak"
    DATA_DISCONNECT = "data_disconnect"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    DAILY_SUMMARY = "daily_summary"


# -----------------------------------------------------------------------------
# Base
# -----------------------------------------------------------------------------


class _Model(BaseModel):
    """Frozen, strict-about-unknown-keys base for every config model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# -----------------------------------------------------------------------------
# Sections
# -----------------------------------------------------------------------------


class AccountConfig(_Model):
    data_feed: DataFeed = DataFeed.IEX
    asset_class: AssetClass = AssetClass.US_EQUITY


class UniverseConfig(_Model):
    symbols: list[str] = Field(min_length=1)

    @field_validator("symbols")
    @classmethod
    def _normalise(cls, symbols: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                msg = "empty symbol in universe"
                raise ValueError(msg)
            seen[symbol] = None
        return list(seen)


class SessionConfig(_Model):
    trade_regular_hours: bool = True
    trade_extended_hours: bool = False
    warmup_multiplier: float = Field(default=1.5, ge=1.0, le=5.0)

    @model_validator(mode="after")
    def _at_least_one_window(self) -> Self:
        if not self.trade_regular_hours and not self.trade_extended_hours:
            msg = "session trades neither regular nor extended hours"
            raise ValueError(msg)
        return self


_TIMEFRAME_RE: Final = re.compile(r"^(\d+)(Min|Hour|Day)$")
_CLASS_PATH_RE: Final = re.compile(r"^[\w.]+:[A-Za-z_]\w*$")

# Risk fields a strategy may override. Every one is an upper bound, so an
# override is only ever accepted when it is stricter (ADR 0003).
_OVERRIDABLE: Final = frozenset(
    {
        "max_position_pct",
        "max_order_notional_pct",
        "max_order_pct_adv",
        "max_open_positions",
        "max_spread_bps",
    }
)


class StrategyConfig(_Model):
    id: str = Field(min_length=1)
    enabled: bool = True
    class_: str = Field(alias="class")
    symbols: list[str] = Field(min_length=1)
    timeframe: str
    params: dict[str, Any] = Field(default_factory=dict)
    risk_overrides: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @field_validator("timeframe")
    @classmethod
    def _known_timeframe(cls, value: str) -> str:
        if not _TIMEFRAME_RE.match(value):
            msg = f"timeframe {value!r} is not of the form 5Min / 1Hour / 1Day"
            raise ValueError(msg)
        return value

    @field_validator("class_")
    @classmethod
    def _class_path_shape(cls, value: str) -> str:
        # Shape only. The import itself happens in doctor, so that merely
        # reading a config file never executes strategy code.
        if not _CLASS_PATH_RE.match(value):
            msg = f"class {value!r} must be of the form 'package.module:ClassName'"
            raise ValueError(msg)
        return value

    @field_validator("symbols")
    @classmethod
    def _upper(cls, symbols: list[str]) -> list[str]:
        return [s.strip().upper() for s in symbols]

    @field_validator("risk_overrides")
    @classmethod
    def _only_known_overrides(cls, overrides: dict[str, Any]) -> dict[str, Any]:
        unknown = set(overrides) - _OVERRIDABLE
        if unknown:
            allowed = ", ".join(sorted(_OVERRIDABLE))
            msg = (
                f"risk_overrides may not set {sorted(unknown)}; "
                f"overridable limits are: {allowed}"
            )
            raise ValueError(msg)
        return overrides


class SizingConfig(_Model):
    method: SizingMethod
    risk_per_trade_pct: PositivePercent
    atr_period: int = Field(ge=2, le=200)
    atr_multiple: Money = Field(gt=0)

    @model_validator(mode="after")
    def _sane_risk_per_trade(self) -> Self:
        if self.risk_per_trade_pct > Decimal("5"):
            msg = (
                f"risk_per_trade_pct {self.risk_per_trade_pct} exceeds 5%; "
                "that is almost certainly a typo"
            )
            raise ValueError(msg)
        return self


class RiskConfig(_Model):
    """Account-level limits. Nothing here has a default -- see the module docstring."""

    max_daily_loss_pct: PositivePercent
    max_drawdown_pct: PositivePercent
    max_gross_exposure_pct: PositivePercent
    max_net_exposure_pct: PositivePercent
    max_open_positions: int = Field(ge=1, le=100)
    max_position_pct: PositivePercent
    max_order_notional_pct: PositivePercent
    max_order_pct_adv: PositivePercent
    min_price: Money = Field(ge=0)
    max_spread_bps: int = Field(ge=1, le=1000)
    allow_shorts: bool
    pdt_policy: PdtPolicy
    sizing: SizingConfig
    eod_policy: EodPolicy
    flatten_lead_minutes: int = Field(ge=1, le=120)
    strict_reconciliation: bool
    kill_liquidates: bool
    overnight_exposure_pct: Percent | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.max_position_pct > self.max_gross_exposure_pct:
            msg = (
                f"max_position_pct ({self.max_position_pct}) exceeds "
                f"max_gross_exposure_pct ({self.max_gross_exposure_pct})"
            )
            raise ValueError(msg)
        if self.max_net_exposure_pct > self.max_gross_exposure_pct:
            msg = "max_net_exposure_pct cannot exceed max_gross_exposure_pct"
            raise ValueError(msg)
        if self.max_daily_loss_pct > self.max_drawdown_pct:
            msg = (
                "max_daily_loss_pct exceeds max_drawdown_pct, so the drawdown "
                "halt could never fire before the daily halt"
            )
            raise ValueError(msg)
        if self.eod_policy is EodPolicy.HOLD:
            if self.overnight_exposure_pct is None:
                msg = "eod_policy 'hold' requires overnight_exposure_pct"
                raise ValueError(msg)
            if self.overnight_exposure_pct > Decimal("50"):
                msg = (
                    f"overnight_exposure_pct {self.overnight_exposure_pct} "
                    "exceeds the 50% ceiling for eod_policy 'hold'"
                )
                raise ValueError(msg)
        return self


class ExecutionConfig(_Model):
    default_order_type: OrderType = OrderType.LIMIT
    limit_offset_bps: int = Field(default=5, ge=0, le=500)
    order_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    reprice_attempts: int = Field(default=2, ge=0, le=10)
    use_bracket_orders: bool = True
    emergency_broker_stop_pct: PositivePercent = Decimal("10.0")
    orphan_position_policy: OrphanPositionPolicy = OrphanPositionPolicy.FLATTEN


class DataConfig(_Model):
    stale_threshold_seconds: int = Field(default=90, ge=5, le=3600)
    max_bar_move_pct: PositivePercent = Decimal("20.0")
    reconnect_max_seconds: int = Field(default=300, ge=10, le=3600)
    disconnect_policy: DisconnectPolicy = DisconnectPolicy.HALT
    record_live_bars: bool = True


class ReconciliationConfig(_Model):
    on_startup: bool = True
    interval_minutes: int = Field(default=15, ge=1, le=240)

    @model_validator(mode="after")
    def _startup_required(self) -> Self:
        if not self.on_startup:
            msg = (
                "reconciliation.on_startup cannot be disabled; startup "
                "reconciliation is how the bot learns what it already holds"
            )
            raise ValueError(msg)
        return self


class AlertsConfig(_Model):
    webhook_env: str = "ALERT_WEBHOOK_URL"
    # Deliberately not called `on`: YAML 1.1 parses a bare `on:` key as the
    # boolean True, so the field would be unusable without quoting it.
    events: list[AlertEvent] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def _critical_present(cls, events: list[AlertEvent]) -> list[AlertEvent]:
        required = {AlertEvent.KILL_SWITCH, AlertEvent.RISK_HALT}
        missing = required - set(events)
        if missing:
            names = ", ".join(sorted(e.value for e in missing))
            msg = f"alerts.events must include the critical events: {names}"
            raise ValueError(msg)
        return events


class ObservabilityConfig(_Model):
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    log_dir: Path = Path("./logs")
    metrics_port: int = Field(default=9090, ge=1024, le=65535)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            msg = f"unknown log level {value!r}"
            raise ValueError(msg)
        return level


class StorageConfig(_Model):
    journal_url: str = "sqlite:///./data/journal.db"
    bars_dir: Path = Path("./data/bars")


# -----------------------------------------------------------------------------
# Root
# -----------------------------------------------------------------------------


class AppConfig(_Model):
    mode: Mode
    account: AccountConfig = Field(default_factory=AccountConfig)
    universe: UniverseConfig
    session: SessionConfig = Field(default_factory=SessionConfig)
    strategies: list[StrategyConfig] = Field(default_factory=list)
    risk: RiskConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @property
    def enabled_strategies(self) -> list[StrategyConfig]:
        return [s for s in self.strategies if s.enabled]

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE

    @model_validator(mode="after")
    def _unique_strategy_ids(self) -> Self:
        seen: set[str] = set()
        for strategy in self.strategies:
            if strategy.id in seen:
                msg = f"duplicate strategy id {strategy.id!r}"
                raise ValueError(msg)
            seen.add(strategy.id)
        return self

    @model_validator(mode="after")
    def _strategy_symbols_in_universe(self) -> Self:
        universe = set(self.universe.symbols)
        for strategy in self.enabled_strategies:
            missing = sorted(set(strategy.symbols) - universe)
            if missing:
                msg = (
                    f"strategy {strategy.id!r} trades {missing}, "
                    "which are not in universe.symbols"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _overrides_only_tighten(self) -> Self:
        """ADR 0003: a strategy may tighten its limits, never loosen them."""
        for strategy in self.strategies:
            for name, raw in strategy.risk_overrides.items():
                account_value = getattr(self.risk, name)
                override = (
                    Decimal(str(raw))
                    if isinstance(account_value, Decimal)
                    else type(account_value)(raw)
                )
                if override > account_value:
                    msg = (
                        f"strategy {strategy.id!r} risk_override {name}={override} "
                        f"is looser than the account limit ({account_value}); "
                        "overrides may only tighten"
                    )
                    raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _extended_hours_order_types(self) -> Self:
        """Alpaca accepts only limit + day orders outside regular hours."""
        if (
            self.session.trade_extended_hours
            and self.execution.default_order_type is not OrderType.LIMIT
        ):
            msg = (
                "trade_extended_hours requires execution.default_order_type "
                "'limit'; Alpaca rejects market orders outside regular hours"
            )
            raise ValueError(msg)
        if self.session.trade_extended_hours and self.execution.use_bracket_orders:
            msg = (
                "trade_extended_hours cannot be combined with use_bracket_orders; "
                "Alpaca does not accept bracket orders outside regular hours"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _crypto_coherence(self) -> Self:
        if self.account.asset_class is AssetClass.CRYPTO:
            if self.session.trade_extended_hours:
                msg = "crypto trades 24/7; trade_extended_hours is meaningless"
                raise ValueError(msg)
            if self.risk.pdt_policy is not PdtPolicy.IGNORE:
                msg = "crypto is not subject to PDT; set pdt_policy 'ignore'"
                raise ValueError(msg)
        return self


# -----------------------------------------------------------------------------
# Secrets
# -----------------------------------------------------------------------------


class Secrets(BaseSettings):
    """Credentials, read from the environment or ``.env``. Never from YAML."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    alpaca_paper_key_id: SecretStr | None = None
    alpaca_paper_secret_key: SecretStr | None = None
    alpaca_live_key_id: SecretStr | None = None
    alpaca_live_secret_key: SecretStr | None = None
    alert_webhook_url: SecretStr | None = None

    def credentials_for(self, mode: Mode) -> tuple[SecretStr, SecretStr]:
        """Return the key pair for ``mode``.

        Paper credentials are never returned for live mode and vice versa.
        There is deliberately no fallback between them.
        """
        if mode is Mode.LIVE:
            key, secret, prefix = (
                self.alpaca_live_key_id,
                self.alpaca_live_secret_key,
                "ALPACA_LIVE",
            )
        else:
            key, secret, prefix = (
                self.alpaca_paper_key_id,
                self.alpaca_paper_secret_key,
                "ALPACA_PAPER",
            )
        if key is None or secret is None:
            msg = (
                f"mode is {mode.value} but {prefix}_KEY_ID / {prefix}_SECRET_KEY "
                "are not set in the environment"
            )
            raise ConfigError(msg)
        return key, secret


# -----------------------------------------------------------------------------
# Loading
# -----------------------------------------------------------------------------


def _guard_live(path: Path, config: AppConfig, *, live_ack: bool) -> None:
    """The redundant gates from docs/configuration.md section 4.

    Four independent things must agree before a live order can be sent: the
    config file's ``mode``, the CLI acknowledgement flag, the config file's
    identity, and the presence of live credentials. This function owns the
    first three; :meth:`Secrets.credentials_for` owns the fourth.
    """
    if not config.is_live:
        return

    if not live_ack:
        msg = (
            f"{path} sets mode: live. Live trading additionally requires "
            "--i-understand-the-risk on the command line."
        )
        raise UnsafeConfigError(msg)

    stem = path.stem.lower()
    if "live" not in stem:
        msg = (
            f"a live config must be named to say so; {path.name!r} does not "
            "contain 'live'. This is what stops a copied paper config from "
            "quietly becoming the live one."
        )
        raise UnsafeConfigError(msg)
    if "paper" in stem or "example" in stem:
        msg = f"{path.name!r} is not an acceptable filename for a live config"
        raise UnsafeConfigError(msg)


def load_config(
    path: Path | str,
    *,
    live_ack: bool = False,
    mode_override: Mode | None = None,
) -> AppConfig:
    """Load, validate, and safety-check a config file.

    Args:
        path: Path to the YAML config.
        live_ack: Whether ``--i-understand-the-risk`` was passed.
        mode_override: ``--mode`` from the CLI. Must agree with the file; this
            is a cross-check, not a way to change what the file says.

    Raises:
        ConfigError: The file is missing, unparseable, or fails validation.
        UnsafeConfigError: The config is valid but unsafe to run as asked.
    """
    path = Path(path)
    if not path.is_file():
        msg = f"config file not found: {path}"
        raise ConfigError(msg)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{path} is not valid YAML: {exc}"
        raise ConfigError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"{path} must contain a YAML mapping at the top level"
        raise ConfigError(msg)

    try:
        config = AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError and friends
        msg = f"{path} failed validation:\n{exc}"
        raise ConfigError(msg) from exc

    if mode_override is not None and mode_override is not config.mode:
        msg = (
            f"--mode {mode_override.value} contradicts {path.name}, which sets "
            f"mode: {config.mode.value}. Edit the file or pass the other config; "
            "the flag will not override it."
        )
        raise UnsafeConfigError(msg)

    _guard_live(path, config, live_ack=live_ack)
    return config
