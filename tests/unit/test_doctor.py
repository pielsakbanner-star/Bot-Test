"""Doctor checks, against a fake broker."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import NOW, FakeBroker, FakeFeedProbe, make_account, make_asset
from tradingbot.broker.protocol import OrderSnapshot, PositionSnapshot
from tradingbot.config import AppConfig, Secrets, load_config
from tradingbot.doctor import Doctor, DoctorReport, Status
from tradingbot.errors import BrokerError


def build(
    config_dict: dict[str, Any],
    write_config: Any,
    *,
    name: str = "paper.yaml",
    live_ack: bool = False,
) -> tuple[AppConfig, Path]:
    path = write_config(config_dict, name)
    return load_config(path, live_ack=live_ack), path


def run_doctor(
    config: AppConfig,
    path: Path,
    secrets: Secrets,
    broker: FakeBroker | None = None,
    probe: FakeFeedProbe | None = None,
) -> DoctorReport:
    return Doctor(
        config,
        secrets,
        broker or FakeBroker(),
        config_path=path,
        feed_probe=probe,
        now=NOW,
    ).run()


def status_of(report: DoctorReport, name: str) -> Status:
    return next(r.status for r in report.results if r.name == name)


def detail_of(report: DoctorReport, name: str) -> str:
    return next(r.detail for r in report.results if r.name == name)


@pytest.fixture
def healthy(
    config_dict: dict[str, Any],
    write_config: Any,
    paper_secrets: Secrets,
    tmp_path: Path,
) -> tuple[AppConfig, Path, Secrets]:
    config_dict["storage"] = {
        "journal_url": f"sqlite:///{tmp_path / 'data' / 'journal.db'}",
        "bars_dir": str(tmp_path / "data" / "bars"),
    }
    config_dict["observability"]["log_dir"] = str(tmp_path / "logs")
    config_dict["strategies"] = []  # no strategy module exists yet in Phase 0
    config, path = build(config_dict, write_config)
    return config, path, paper_secrets


def test_healthy_account_has_no_failures(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets)
    assert report.ok, report.render()
    assert status_of(report, "broker.auth") is Status.PASS
    assert status_of(report, "account.blocks") is Status.PASS
    assert status_of(report, "universe.tradable") is Status.PASS


def test_render_lists_every_check(healthy: Any) -> None:
    config, path, secrets = healthy
    rendered = run_doctor(config, path, secrets).render()
    assert "CHECK" in rendered
    assert "broker.auth" in rendered
    assert "OK:" in rendered


# --- account ------------------------------------------------------------------


def test_auth_failure_stops_early(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(raise_on_account=BrokerError("401 unauthorized"))
    report = run_doctor(config, path, secrets, broker)
    assert not report.ok
    assert status_of(report, "broker.auth") is Status.FAIL
    # No point checking symbols when we cannot even authenticate.
    assert len(report.results) == 3


def test_blocked_account_fails(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(account=make_account(trading_blocked=True))
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "account.blocks") is Status.FAIL
    assert "trading_blocked" in detail_of(report, "account.blocks")


def test_cash_account_fails_for_equities(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(account=make_account(multiplier=1))
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "account.type") is Status.FAIL
    assert "good-faith" in detail_of(report, "account.type")


def test_shorting_mismatch_fails(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["risk"]["allow_shorts"] = True
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    broker = FakeBroker(account=make_account(shorting_enabled=False))
    report = run_doctor(config, path, paper_secrets, broker)
    assert status_of(report, "account.shorting") is Status.FAIL


def test_shorting_check_skipped_when_shorts_disabled(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets)
    assert status_of(report, "account.shorting") is Status.SKIP


# --- PDT ----------------------------------------------------------------------


def test_pdt_ignore_below_threshold_fails(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["risk"]["pdt_policy"] = "ignore"
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    broker = FakeBroker(account=make_account(equity=Decimal("10000")))
    report = run_doctor(config, path, paper_secrets, broker)
    assert status_of(report, "account.pdt") is Status.FAIL
    assert "25000" in detail_of(report, "account.pdt")


def test_pdt_ignore_above_threshold_passes(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["risk"]["pdt_policy"] = "ignore"
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    report = run_doctor(config, path, paper_secrets)
    assert status_of(report, "account.pdt") is Status.PASS


def test_exhausted_day_trade_budget_warns(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(account=make_account(equity=Decimal("10000"), daytrade_count=3))
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "account.pdt") is Status.WARN


def test_crypto_skips_pdt(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["account"]["asset_class"] = "crypto"
    config_dict["risk"]["pdt_policy"] = "ignore"
    config_dict["universe"]["symbols"] = ["BTC/USD"]
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    broker = FakeBroker(assets={"BTC/USD": make_asset("BTC/USD", asset_class="crypto")})
    report = run_doctor(config, path, paper_secrets, broker)
    assert status_of(report, "account.pdt") is Status.SKIP
    assert status_of(report, "market.calendar") is Status.SKIP


# --- clock and calendar -------------------------------------------------------


def test_clock_skew_fails(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(clock_ts=NOW + timedelta(seconds=30))
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "market.clock") is Status.FAIL
    assert "skew" in detail_of(report, "market.clock")


def test_early_close_warns(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets, FakeBroker(early_close=True))
    assert status_of(report, "market.calendar") is Status.WARN
    assert "EARLY CLOSE" in detail_of(report, "market.calendar")


def test_market_holiday_warns(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets, FakeBroker(market_closed_today=True))
    assert status_of(report, "market.calendar") is Status.WARN
    assert "closed today" in detail_of(report, "market.calendar")


# --- data feed ----------------------------------------------------------------


def test_iex_feed_warns_even_when_entitled(healthy: Any) -> None:
    config, path, secrets = healthy
    probe = FakeFeedProbe()
    report = run_doctor(config, path, secrets, probe=probe)
    assert status_of(report, "data.feed") is Status.WARN
    assert probe.calls == [("SPY", config.account.data_feed)]


def test_unentitled_feed_fails(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["account"]["data_feed"] = "sip"
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    probe = FakeFeedProbe(error=BrokerError("subscription does not permit sip"))
    report = run_doctor(config, path, paper_secrets, probe=probe)
    assert status_of(report, "data.feed") is Status.FAIL


def test_feed_check_skipped_without_a_probe(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets)
    assert status_of(report, "data.feed") is Status.SKIP


# --- universe -----------------------------------------------------------------


def test_untradable_symbol_fails(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(
        assets={"SPY": make_asset("SPY"), "QQQ": make_asset("QQQ", tradable=False)}
    )
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "universe.tradable") is Status.FAIL
    assert "QQQ" in detail_of(report, "universe.tradable")


def test_unknown_symbol_fails(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(assets={"SPY": make_asset("SPY")})
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "universe.tradable") is Status.FAIL
    assert "QQQ" in detail_of(report, "universe.tradable")


def test_wrong_asset_class_fails(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(
        assets={
            "SPY": make_asset("SPY"),
            "QQQ": make_asset("QQQ", asset_class="crypto"),
        }
    )
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "universe.tradable") is Status.FAIL


# --- strategies ---------------------------------------------------------------


def test_missing_strategy_module_fails(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config, path = build(config_dict, write_config)
    report = run_doctor(config, path, paper_secrets)
    assert status_of(report, "strategies") is Status.FAIL
    assert "cannot import" in detail_of(report, "strategies")


def test_importable_strategy_passes(
    config_dict: dict[str, Any], write_config: Any, paper_secrets: Secrets
) -> None:
    config_dict["strategies"][0]["class"] = "tradingbot.doctor:Doctor"
    config, path = build(config_dict, write_config)
    report = run_doctor(config, path, paper_secrets)
    assert status_of(report, "strategies") is Status.PASS


def test_no_strategies_warns(healthy: Any) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets)
    assert status_of(report, "strategies") is Status.WARN


# --- existing state -----------------------------------------------------------


def test_existing_positions_warn(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(
        positions=[
            PositionSnapshot(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("500.00"),
                market_value=Decimal("5010.00"),
                unrealized_pl=Decimal("10.00"),
            )
        ]
    )
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "state.positions") is Status.WARN
    assert "SPY 10" in detail_of(report, "state.positions")


def test_existing_open_orders_warn(healthy: Any) -> None:
    config, path, secrets = healthy
    broker = FakeBroker(
        orders=[
            OrderSnapshot(
                order_id="o1",
                client_order_id="sma-SPY-1-abcd",
                symbol="SPY",
                side="buy",
                qty=Decimal("5"),
                filled_qty=Decimal("0"),
                order_type="limit",
                status="new",
                submitted_at=NOW,
            )
        ]
    )
    report = run_doctor(config, path, secrets, broker)
    assert status_of(report, "state.orders") is Status.WARN


# --- alerts -------------------------------------------------------------------


def test_live_without_webhook_fails(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["mode"] = "live"
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config, name="live.yaml", live_ack=True)
    secrets = Secrets(
        _env_file=None,
        alpaca_live_key_id="AKID000000",
        alpaca_live_secret_key="live-secret-0123456789",
    )
    report = run_doctor(config, path, secrets)
    assert status_of(report, "alerts") is Status.FAIL
    assert "live mode requires" in detail_of(report, "alerts")


def test_paper_without_webhook_only_warns(
    config_dict: dict[str, Any], write_config: Any
) -> None:
    config_dict["strategies"] = []
    config, path = build(config_dict, write_config)
    secrets = Secrets(
        _env_file=None,
        alpaca_paper_key_id="PKID000000",
        alpaca_paper_secret_key="paper-secret-0123456789",
    )
    report = run_doctor(config, path, secrets)
    assert status_of(report, "alerts") is Status.WARN


# --- storage ------------------------------------------------------------------


def test_storage_paths_are_created(healthy: Any, tmp_path: Path) -> None:
    config, path, secrets = healthy
    report = run_doctor(config, path, secrets)
    assert status_of(report, "storage.writable") is Status.PASS
    assert (tmp_path / "data" / "bars").is_dir()
