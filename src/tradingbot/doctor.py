"""The `doctor` command.

Runs the startup validation table from docs/configuration.md section 3 plus the
pre-market items from docs/operations.md section 2, and prints a pass/fail
table. It places no orders and mutates nothing.

Written against :class:`~tradingbot.broker.protocol.BrokerReader` rather than
the Alpaca SDK, so the whole check suite is testable against a fake.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from tradingbot.broker.protocol import AccountSnapshot, BrokerReader
from tradingbot.config import (
    AppConfig,
    AssetClass,
    DataFeed,
    Mode,
    PdtPolicy,
    Secrets,
)
from tradingbot.errors import BrokerError

MAX_CLOCK_SKEW: Final = timedelta(seconds=2)
PDT_EQUITY_THRESHOLD: Final = Decimal("25000")
# FINRA allows 3 day trades per rolling 5 business days below the threshold.
PDT_MAX_DAY_TRADES: Final = 3
# How many positions to name before summarising the rest.
POSITION_SUMMARY_LIMIT: Final = 5


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: list[CheckResult]

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.FAIL]

    @property
    def warned(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.WARN]

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL", "-" * (width + 40)]
        lines.extend(
            f"{r.name.ljust(width)}  {r.status.value:<6}  {r.detail}"
            for r in self.results
        )
        lines.append("")
        if self.ok:
            summary = f"{len(self.results)} checks, 0 failures"
            if self.warned:
                summary += f", {len(self.warned)} warning(s)"
            lines.append(f"OK: {summary}")
        else:
            names = ", ".join(r.name for r in self.failed)
            lines.append(f"FAILED: {len(self.failed)} check(s): {names}")
        return "\n".join(lines)


class FeedProbe(Protocol):
    """Confirms the configured market-data feed is actually entitled."""

    def probe(self, symbol: str, feed: DataFeed) -> None:
        """Raise :class:`BrokerError` if the feed is not usable."""


class Doctor:
    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets,
        broker: BrokerReader,
        *,
        config_path: Path,
        feed_probe: FeedProbe | None = None,
        now: datetime | None = None,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._broker = broker
        self._config_path = config_path
        self._feed_probe = feed_probe
        self._now = now

    def run(self) -> DoctorReport:
        results: list[CheckResult] = [
            self._check_config(),
            self._check_credentials(),
        ]

        try:
            account = self._broker.get_account()
        except BrokerError as exc:
            results.append(CheckResult("broker.auth", Status.FAIL, str(exc)))
            return DoctorReport(results)

        results.append(
            CheckResult(
                "broker.auth",
                Status.PASS,
                f"authenticated, account {account.account_number}",
            )
        )
        results.extend(
            [
                self._check_blocks(account),
                self._check_account_type(account),
                self._check_pdt(account),
                self._check_shorting(account),
                *self._check_clock(),
                self._check_feed(),
                *self._check_symbols(),
                *self._check_strategies(),
                *self._check_existing_state(),
                self._check_storage(),
                self._check_alerts(),
            ]
        )
        return DoctorReport(results)

    # --- config and credentials ---------------------------------------------

    def _check_config(self) -> CheckResult:
        cfg = self._config
        return CheckResult(
            "config",
            Status.PASS,
            f"{self._config_path.name}: mode={cfg.mode.value}, "
            f"{len(cfg.universe.symbols)} symbols, "
            f"{len(cfg.enabled_strategies)} enabled strateg"
            f"{'y' if len(cfg.enabled_strategies) == 1 else 'ies'}",
        )

    def _check_credentials(self) -> CheckResult:
        prefix = "ALPACA_LIVE" if self._config.is_live else "ALPACA_PAPER"
        # credentials_for raises if absent; reaching Doctor means it did not.
        self._secrets.credentials_for(self._config.mode)
        return CheckResult("credentials", Status.PASS, f"{prefix}_* present")

    # --- account -------------------------------------------------------------

    def _check_blocks(self, account: AccountSnapshot) -> CheckResult:
        blocked = [
            name
            for name in ("trading_blocked", "account_blocked", "transfers_blocked")
            if getattr(account, name)
        ]
        if blocked:
            return CheckResult(
                "account.blocks",
                Status.FAIL,
                f"account is blocked: {', '.join(blocked)}",
            )
        return CheckResult("account.blocks", Status.PASS, "no blocks")

    def _check_account_type(self, account: AccountSnapshot) -> CheckResult:
        if (
            account.is_cash_account
            and self._config.account.asset_class is AssetClass.US_EQUITY
        ):
            return CheckResult(
                "account.type",
                Status.FAIL,
                "cash account (multiplier 1): intraday round trips risk "
                "good-faith violations; a margin account is required",
            )
        return CheckResult(
            "account.type",
            Status.PASS,
            f"multiplier {account.multiplier}, equity {account.equity}",
        )

    def _check_pdt(self, account: AccountSnapshot) -> CheckResult:
        policy = self._config.risk.pdt_policy
        if self._config.account.asset_class is AssetClass.CRYPTO:
            return CheckResult(
                "account.pdt", Status.SKIP, "crypto is not subject to PDT"
            )

        equity = account.equity
        flagged = account.pattern_day_trader
        count = account.daytrade_count
        detail = f"equity {equity}, daytrade_count {count}, flagged={flagged}"

        if policy is PdtPolicy.IGNORE and equity < PDT_EQUITY_THRESHOLD and not flagged:
            return CheckResult(
                "account.pdt",
                Status.FAIL,
                f"pdt_policy 'ignore' requires equity >= {PDT_EQUITY_THRESHOLD} "
                f"or an already-flagged margin account; {detail}",
            )
        if equity < PDT_EQUITY_THRESHOLD and count >= PDT_MAX_DAY_TRADES:
            return CheckResult(
                "account.pdt", Status.WARN, f"day-trade budget exhausted; {detail}"
            )
        return CheckResult("account.pdt", Status.PASS, detail)

    def _check_shorting(self, account: AccountSnapshot) -> CheckResult:
        if not self._config.risk.allow_shorts:
            return CheckResult("account.shorting", Status.SKIP, "allow_shorts is false")
        if not account.shorting_enabled:
            return CheckResult(
                "account.shorting",
                Status.FAIL,
                "allow_shorts is true but the account cannot short",
            )
        return CheckResult("account.shorting", Status.PASS, "shorting enabled")

    # --- clock and calendar --------------------------------------------------

    def _check_clock(self) -> list[CheckResult]:
        try:
            clock = self._broker.get_clock()
        except BrokerError as exc:
            return [CheckResult("market.clock", Status.FAIL, str(exc))]

        local = self._now or datetime.now(UTC)
        skew = abs(clock.timestamp - local)
        skew_status = Status.PASS if skew <= MAX_CLOCK_SKEW else Status.FAIL
        results = [
            CheckResult(
                "market.clock",
                skew_status,
                f"broker clock skew {skew.total_seconds():.2f}s "
                f"(limit {MAX_CLOCK_SKEW.total_seconds():.0f}s), "
                f"market {'open' if clock.is_open else 'closed'}",
            )
        ]

        if self._config.account.asset_class is AssetClass.CRYPTO:
            results.append(
                CheckResult("market.calendar", Status.SKIP, "crypto trades 24/7")
            )
            return results

        today = clock.timestamp.date()
        try:
            days = self._broker.get_calendar(today, today + timedelta(days=5))
        except BrokerError as exc:
            results.append(CheckResult("market.calendar", Status.FAIL, str(exc)))
            return results

        today_session = next((d for d in days if d.session_date == today), None)
        if today_session is None:
            nxt = days[0].session_date.isoformat() if days else "unknown"
            results.append(
                CheckResult(
                    "market.calendar", Status.WARN, f"market closed today; next {nxt}"
                )
            )
        elif today_session.is_early_close:
            results.append(
                CheckResult(
                    "market.calendar",
                    Status.WARN,
                    f"EARLY CLOSE today at {today_session.close_at:%H:%M %Z} -- "
                    "the flatten window moves with it",
                )
            )
        else:
            results.append(
                CheckResult(
                    "market.calendar",
                    Status.PASS,
                    f"session {today_session.open_at:%H:%M}-"
                    f"{today_session.close_at:%H:%M %Z}",
                )
            )
        return results

    # --- data ----------------------------------------------------------------

    def _check_feed(self) -> CheckResult:
        feed = self._config.account.data_feed
        if self._feed_probe is None:
            return CheckResult("data.feed", Status.SKIP, f"{feed.value} (not probed)")
        symbol = self._config.universe.symbols[0]
        try:
            self._feed_probe.probe(symbol, feed)
        except BrokerError as exc:
            return CheckResult(
                "data.feed",
                Status.FAIL,
                f"{feed.value} feed not usable for {symbol}: {exc}",
            )
        if feed is DataFeed.IEX:
            return CheckResult(
                "data.feed",
                Status.WARN,
                "iex is IEX-only (a few percent of consolidated volume); "
                "do not go live on an edge thinner than the IEX/SIP difference",
            )
        return CheckResult("data.feed", Status.PASS, "sip entitled")

    def _check_symbols(self) -> list[CheckResult]:
        expected = (
            "crypto"
            if self._config.account.asset_class is AssetClass.CRYPTO
            else "us_equity"
        )
        problems: list[str] = []
        notes: list[str] = []
        for symbol in self._config.universe.symbols:
            try:
                asset = self._broker.get_asset(symbol)
            except BrokerError as exc:
                problems.append(f"{symbol}: {exc}")
                continue
            if not asset.tradable or asset.status.lower() != "active":
                problems.append(
                    f"{symbol}: status={asset.status} tradable={asset.tradable}"
                )
                continue
            if expected not in asset.asset_class.lower():
                problems.append(
                    f"{symbol}: asset_class {asset.asset_class}, expected {expected}"
                )
                continue
            if self._config.risk.allow_shorts and not asset.shortable:
                notes.append(f"{symbol} not shortable")

        results = [
            CheckResult(
                "universe.tradable",
                Status.FAIL if problems else Status.PASS,
                "; ".join(problems)
                if problems
                else f"all {len(self._config.universe.symbols)} symbols tradable",
            )
        ]
        if notes:
            results.append(
                CheckResult("universe.shortable", Status.WARN, "; ".join(notes))
            )
        return results

    # --- strategies ----------------------------------------------------------

    def _check_strategies(self) -> list[CheckResult]:
        enabled = self._config.enabled_strategies
        if not enabled:
            return [
                CheckResult(
                    "strategies",
                    Status.WARN,
                    "no enabled strategies; nothing will trade",
                )
            ]
        problems: list[str] = []
        for strategy in enabled:
            module_path, _, attr = strategy.class_.partition(":")
            try:
                module = importlib.import_module(module_path)
            except ImportError as exc:
                problems.append(f"{strategy.id}: cannot import {module_path} ({exc})")
                continue
            if not hasattr(module, attr):
                problems.append(f"{strategy.id}: {module_path} has no {attr}")
        return [
            CheckResult(
                "strategies",
                Status.FAIL if problems else Status.PASS,
                "; ".join(problems) if problems else f"{len(enabled)} importable",
            )
        ]

    # --- existing state ------------------------------------------------------

    def _check_existing_state(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        try:
            positions = self._broker.list_positions()
        except BrokerError as exc:
            results.append(CheckResult("state.positions", Status.FAIL, str(exc)))
        else:
            if positions:
                summary = ", ".join(
                    f"{p.symbol} {p.qty}" for p in positions[:POSITION_SUMMARY_LIMIT]
                )
                extra = len(positions) - POSITION_SUMMARY_LIMIT
                more = f" (+{extra} more)" if extra > 0 else ""
                results.append(
                    CheckResult(
                        "state.positions",
                        Status.WARN,
                        f"{len(positions)} open position(s) before start: "
                        f"{summary}{more}",
                    )
                )
            else:
                results.append(CheckResult("state.positions", Status.PASS, "flat"))

        try:
            orders = self._broker.list_open_orders()
        except BrokerError as exc:
            results.append(CheckResult("state.orders", Status.FAIL, str(exc)))
        else:
            results.append(
                CheckResult(
                    "state.orders",
                    Status.WARN if orders else Status.PASS,
                    f"{len(orders)} open order(s)" if orders else "none",
                )
            )
        return results

    # --- local -------------------------------------------------------------

    def _check_storage(self) -> CheckResult:
        storage = self._config.storage
        targets: list[Path] = [
            Path(storage.bars_dir),
            Path(self._config.observability.log_dir),
        ]
        if storage.journal_url.startswith("sqlite:///"):
            targets.append(Path(storage.journal_url.removeprefix("sqlite:///")).parent)
        problems: list[str] = []
        for target in targets:
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".doctor-write-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                problems.append(f"{target}: {exc}")
        return CheckResult(
            "storage.writable",
            Status.FAIL if problems else Status.PASS,
            "; ".join(problems) if problems else f"{len(targets)} path(s) writable",
        )

    def _check_alerts(self) -> CheckResult:
        alerts = self._config.observability.alerts
        configured = self._secrets.has_alert_webhook
        if self._config.mode is Mode.LIVE and not configured:
            return CheckResult(
                "alerts",
                Status.FAIL,
                f"live mode requires an alert channel; "
                f"{alerts.webhook_env} is empty or unset",
            )
        if not configured:
            return CheckResult(
                "alerts",
                Status.WARN,
                f"{alerts.webhook_env} unset; alerts go to logs only",
            )
        return CheckResult(
            "alerts", Status.PASS, f"{len(alerts.events)} event type(s) -> webhook"
        )
