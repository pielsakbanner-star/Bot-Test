"""Quality gates and the staleness watchdog.

Every test here is about refusing to act on bad data, or about the asymmetry
that lets you exit a position when you can no longer see the market.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.data.quality import (
    QualityLimits,
    SymbolQuality,
    Verdict,
    check_bar,
)
from tradingbot.data.staleness import StalenessWatchdog
from tradingbot.data.types import ONE_MINUTE, Bar

T0 = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
LIMITS = QualityLimits(max_bar_move_pct=Decimal("20"))


def bar(
    minute: int = 0,
    *,
    o: str = "100",
    h: str = "101",
    low: str = "99",
    c: str = "100",
    volume: str = "1000",
) -> Bar:
    return Bar(
        symbol="SPY",
        timeframe=ONE_MINUTE,
        timestamp=T0 + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(volume),
    )


# --- structural rejects -------------------------------------------------------


def test_clean_bar_is_accepted() -> None:
    assert check_bar(bar(), None, LIMITS).verdict is Verdict.ACCEPT


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"o": "0"}, "non-positive"),
        ({"c": "-1"}, "non-positive"),
        ({"volume": "-5"}, "negative volume"),
        ({"h": "99", "c": "100", "low": "98"}, "impossible OHLC"),
        ({"low": "101", "o": "100", "h": "102", "c": "100"}, "impossible OHLC"),
    ],
)
def test_structurally_impossible_bars_are_rejected(
    kwargs: dict[str, str], reason: str
) -> None:
    result = check_bar(bar(**kwargs), None, LIMITS)  # type: ignore[arg-type]
    assert result.rejected
    assert reason in result.reason


def test_out_of_order_bar_is_rejected() -> None:
    previous = bar(5)
    result = check_bar(bar(3), previous, LIMITS)
    assert result.rejected
    assert "out of order" in result.reason


def test_duplicate_timestamp_is_rejected() -> None:
    previous = bar(5)
    assert check_bar(bar(5), previous, LIMITS).rejected


# --- suspect (not rejected) ---------------------------------------------------


def test_implausible_move_is_suspect_not_rejected() -> None:
    """A 30% bar might be real -- a halt resumption, a takeover. It is flagged
    so entries stop, not dropped as if it never happened."""
    previous = bar(0, c="100")
    result = check_bar(bar(1, o="130", h="131", low="129", c="130"), previous, LIMITS)
    assert result.verdict is Verdict.SUSPECT
    assert not result.rejected
    assert "30.0%" in result.reason


def test_move_at_the_limit_is_accepted() -> None:
    previous = bar(0, c="100")
    result = check_bar(bar(1, o="120", h="121", low="119", c="120"), previous, LIMITS)
    assert result.verdict is Verdict.ACCEPT


def test_downward_move_is_measured_by_magnitude() -> None:
    previous = bar(0, c="100")
    result = check_bar(bar(1, o="70", h="71", low="69", c="70"), previous, LIMITS)
    assert result.verdict is Verdict.SUSPECT


# --- per-symbol state ---------------------------------------------------------


def test_three_suspect_bars_disable_the_symbol() -> None:
    quality = SymbolQuality("SPY", max_suspect_bars=3)
    suspect = check_bar(
        bar(1, o="200", h="201", low="199", c="200"), bar(0, c="100"), LIMITS
    )
    for _ in range(2):
        quality.record(suspect)
        assert not quality.disabled
    quality.record(suspect)
    assert quality.disabled
    assert "3 suspect bars" in quality.disabled_reason


def test_clean_bars_do_not_reset_the_suspect_count() -> None:
    """Three bad bars in a session is a symbol worth distrusting for the rest
    of it, even with good bars in between."""
    quality = SymbolQuality("SPY", max_suspect_bars=3)
    suspect = check_bar(
        bar(1, o="200", h="201", low="199", c="200"), bar(0, c="100"), LIMITS
    )
    clean = check_bar(bar(), None, LIMITS)
    quality.record(suspect)
    quality.record(clean)
    quality.record(suspect)
    quality.record(clean)
    quality.record(suspect)
    assert quality.disabled


def test_rejects_are_counted_separately() -> None:
    quality = SymbolQuality("SPY")
    quality.record(check_bar(bar(o="0"), None, LIMITS))
    assert quality.reject_count == 1
    assert quality.suspect_count == 0
    assert not quality.disabled


# --- staleness ----------------------------------------------------------------


def test_symbol_goes_stale_after_the_threshold() -> None:
    watchdog = StalenessWatchdog(threshold=timedelta(seconds=90))
    watchdog.track(["SPY"], T0)
    assert not watchdog.is_stale("SPY", T0 + timedelta(seconds=89))
    assert watchdog.is_stale("SPY", T0 + timedelta(seconds=91))


def test_a_fresh_bar_clears_staleness() -> None:
    watchdog = StalenessWatchdog(threshold=timedelta(seconds=90))
    watchdog.track(["SPY"], T0)
    later = T0 + timedelta(seconds=120)
    assert watchdog.is_stale("SPY", later)
    watchdog.record_bar("SPY", later)
    assert not watchdog.is_stale("SPY", later)


def test_widespread_staleness_is_flagged_as_a_connection_problem() -> None:
    """One symbol quiet is a symbol; a quarter of them quiet is the link."""
    watchdog = StalenessWatchdog(threshold=timedelta(seconds=90), alert_ratio=0.25)
    symbols = ["SPY", "QQQ", "AAPL", "MSFT"]
    watchdog.track(symbols, T0)
    later = T0 + timedelta(seconds=120)
    for symbol in ["SPY", "QQQ", "AAPL"]:
        watchdog.record_bar(symbol, later)

    report = watchdog.evaluate(later)
    assert report.stale == ("MSFT",)
    assert report.likely_connection_problem  # 1 of 4 == 25%


def test_one_stale_symbol_in_a_large_universe_is_not_a_connection_problem() -> None:
    watchdog = StalenessWatchdog(threshold=timedelta(seconds=90), alert_ratio=0.25)
    symbols = [f"S{i}" for i in range(10)]
    watchdog.track(symbols, T0)
    later = T0 + timedelta(seconds=120)
    for symbol in symbols[:-1]:
        watchdog.record_bar(symbol, later)

    report = watchdog.evaluate(later)
    assert report.stale == ("S9",)
    assert not report.likely_connection_problem


def test_disconnect_marks_everything_stale_without_lying_about_age() -> None:
    """Forcing a flag rather than back-dating keeps `age()` honest for the log
    line that explains the halt."""
    watchdog = StalenessWatchdog()
    watchdog.track(["SPY", "QQQ"], T0)
    watchdog.record_bar("SPY", T0)

    watchdog.mark_all_stale()
    assert watchdog.is_stale("SPY", T0)
    assert watchdog.age("SPY", T0) == timedelta(0)

    watchdog.record_bar("SPY", T0 + timedelta(seconds=1))
    assert not watchdog.is_stale("SPY", T0 + timedelta(seconds=1))
    assert watchdog.is_stale("QQQ", T0 + timedelta(seconds=1))


def test_untracked_symbol_is_not_stale() -> None:
    watchdog = StalenessWatchdog()
    assert not watchdog.is_stale("NOPE", T0)
    assert watchdog.age("NOPE", T0) is None


def test_evaluate_on_an_empty_universe_is_quiet() -> None:
    report = StalenessWatchdog().evaluate(T0)
    assert not report.any_stale
    assert not report.likely_connection_problem
