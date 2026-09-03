"""Bar aggregation, timeframes, and the rolling window.

Session-boundary alignment and gap filling are where a bar pipeline goes
quietly wrong, so most of these tests are about those two things.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.data.aggregator import BarAggregator, MultiTimeframeAggregator
from tradingbot.data.types import ONE_MINUTE, Bar, BarWindow, TimeFrame, TimeFrameUnit

# 2026-09-03 09:30 America/New_York == 13:30 UTC
SESSION_OPEN = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
FIVE_MIN = TimeFrame(5, TimeFrameUnit.MINUTE)


def minute_bar(
    offset: int,
    close: str = "100",
    *,
    symbol: str = "SPY",
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000",
) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timeframe=ONE_MINUTE,
        timestamp=SESSION_OPEN + timedelta(minutes=offset),
        open=price,
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        close=price,
        volume=Decimal(volume),
        trade_count=10,
    )


# --- TimeFrame ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("1Min", 1, TimeFrameUnit.MINUTE),
        ("5Min", 5, TimeFrameUnit.MINUTE),
        ("1Hour", 1, TimeFrameUnit.HOUR),
        ("1Day", 1, TimeFrameUnit.DAY),
    ],
)
def test_timeframe_parsing(text: str, amount: int, unit: TimeFrameUnit) -> None:
    tf = TimeFrame.parse(text)
    assert tf == TimeFrame(amount, unit)
    assert str(tf) == text


@pytest.mark.parametrize("text", ["5m", "min", "0Min", "5 Min", "5Minutes", ""])
def test_bad_timeframes_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="timeframe|positive"):
        TimeFrame.parse(text)


def test_floor_aligns_to_the_session_open_not_the_wall_clock() -> None:
    """The whole point: a 5-minute bar runs 09:30-09:35, not 09:32-09:37."""
    moment = SESSION_OPEN + timedelta(minutes=7, seconds=30)
    assert FIVE_MIN.floor(moment, origin=SESSION_OPEN) == SESSION_OPEN + timedelta(
        minutes=5
    )


def test_floor_at_an_exact_boundary_is_a_fixed_point() -> None:
    boundary = SESSION_OPEN + timedelta(minutes=10)
    assert FIVE_MIN.floor(boundary, origin=SESSION_OPEN) == boundary


def test_floor_before_the_origin_is_an_error() -> None:
    with pytest.raises(ValueError, match="earlier than the origin"):
        FIVE_MIN.floor(SESSION_OPEN - timedelta(minutes=1), origin=SESSION_OPEN)


# --- aggregation --------------------------------------------------------------


def test_bar_seals_on_the_first_tick_of_the_next_interval() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    for offset in range(5):
        assert agg.add(minute_bar(offset)) == []
    sealed = agg.add(minute_bar(5))
    assert len(sealed) == 1
    assert sealed[0].timestamp == SESSION_OPEN
    assert sealed[0].timeframe == FIVE_MIN


def test_sealed_bar_aggregates_ohlcv_correctly() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0, "100", high="101", low="99"))
    agg.add(minute_bar(1, "103", high="105", low="102"))
    agg.add(minute_bar(2, "102", high="104", low="98"))
    (bar,) = agg.add(minute_bar(5))

    assert bar.open == Decimal("100")  # first bar's open
    assert bar.high == Decimal("105")  # highest high
    assert bar.low == Decimal("98")  # lowest low
    assert bar.close == Decimal("102")  # last bar's close
    assert bar.volume == Decimal("3000")
    assert bar.trade_count == 30


def test_vwap_is_volume_weighted() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0, "100", volume="1000"))
    agg.add(minute_bar(1, "200", volume="3000"))
    (bar,) = agg.add(minute_bar(5))
    # (100*1000 + 200*3000) / 4000 == 175
    assert bar.vwap == Decimal("175")


def test_bar_seals_on_the_grace_timer_when_the_symbol_goes_quiet() -> None:
    """A symbol that stops printing must still seal, or its bar never closes."""
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0))

    too_early = SESSION_OPEN + timedelta(minutes=5, seconds=1)
    assert agg.flush(too_early) == []

    after_grace = SESSION_OPEN + timedelta(minutes=5, seconds=3)
    sealed = agg.flush(after_grace)
    assert len(sealed) == 1
    assert sealed[0].timestamp == SESSION_OPEN


def test_flush_twice_does_not_double_seal() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0))
    after = SESSION_OPEN + timedelta(minutes=6)
    assert len(agg.flush(after)) == 1
    assert agg.flush(after) == []


def test_missing_intervals_become_synthetic_flat_bars() -> None:
    """A hole in the window is worse than a manufactured flat bar, as long as
    the manufactured one is labelled."""
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0, "100"))
    sealed = agg.add(minute_bar(20, "110"))  # skips 09:35, 09:40, 09:45

    assert [b.timestamp for b in sealed] == [
        SESSION_OPEN,
        SESSION_OPEN + timedelta(minutes=5),
        SESSION_OPEN + timedelta(minutes=10),
        SESSION_OPEN + timedelta(minutes=15),
    ]
    real, *synthetic = sealed
    assert not real.synthetic
    assert all(b.synthetic for b in synthetic)
    assert all(b.volume == 0 for b in synthetic)
    assert all(b.is_flat for b in synthetic)
    assert all(b.close == Decimal("100") for b in synthetic)


def test_gap_filling_stops_at_the_session_close() -> None:
    agg = BarAggregator(
        "SPY",
        FIVE_MIN,
        session_open=SESSION_OPEN,
        session_close=SESSION_OPEN + timedelta(minutes=15),
    )
    agg.add(minute_bar(0))
    sealed = agg.add(minute_bar(30))
    assert all(b.timestamp < SESSION_OPEN + timedelta(minutes=15) for b in sealed)


def test_late_bar_for_a_sealed_interval_is_dropped() -> None:
    """The sealed bar has already been acted on; rewriting history is worse."""
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0))
    agg.add(minute_bar(6))  # seals 09:30, opens 09:35
    assert agg.add(minute_bar(2)) == []
    assert agg.pending_start == SESSION_OPEN + timedelta(minutes=5)


def test_close_session_seals_a_short_final_bar() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    agg.add(minute_bar(0))
    agg.add(minute_bar(1))
    (bar,) = agg.close_session()
    assert bar.timestamp == SESSION_OPEN
    assert agg.close_session() == []


def test_aggregator_rejects_wrong_inputs() -> None:
    agg = BarAggregator("SPY", FIVE_MIN, session_open=SESSION_OPEN)
    with pytest.raises(ValueError, match="1Min bars"):
        agg.add(
            Bar(
                symbol="SPY",
                timeframe=FIVE_MIN,
                timestamp=SESSION_OPEN,
                open=Decimal(1),
                high=Decimal(1),
                low=Decimal(1),
                close=Decimal(1),
                volume=Decimal(1),
            )
        )
    with pytest.raises(ValueError, match="fed to SPY"):
        agg.add(minute_bar(0, symbol="QQQ"))
    with pytest.raises(ValueError, match="precedes the session open"):
        agg.add(minute_bar(-1))


def test_hourly_aggregation_uses_the_session_origin() -> None:
    """An hourly bar from a 09:30 open runs 09:30-10:30, not 09:00-10:00."""
    hourly = TimeFrame(1, TimeFrameUnit.HOUR)
    agg = BarAggregator("SPY", hourly, session_open=SESSION_OPEN)
    agg.add(minute_bar(0, "100"))
    assert agg.add(minute_bar(59, "110")) == []  # 10:29 ET, last minute inside
    (bar,) = agg.add(minute_bar(60, "111"))  # 10:30 ET opens the next hour
    assert bar.timestamp == SESSION_OPEN
    assert bar.end == SESSION_OPEN + timedelta(hours=1)
    assert bar.close == Decimal("110")


# --- multiple timeframes ------------------------------------------------------


def test_multi_timeframe_fans_out() -> None:
    agg = MultiTimeframeAggregator(
        "SPY", [ONE_MINUTE, FIVE_MIN], session_open=SESSION_OPEN
    )
    out = agg.add(minute_bar(0))
    assert [b.timeframe for b in out] == [ONE_MINUTE]  # passthrough only

    for offset in range(1, 5):
        agg.add(minute_bar(offset))
    out = agg.add(minute_bar(5))
    assert sorted(str(b.timeframe) for b in out) == ["1Min", "5Min"]


def test_multi_timeframe_without_passthrough_emits_nothing_early() -> None:
    agg = MultiTimeframeAggregator("SPY", [FIVE_MIN], session_open=SESSION_OPEN)
    assert agg.add(minute_bar(0)) == []


# --- BarWindow ----------------------------------------------------------------


def test_window_evicts_oldest_when_full() -> None:
    window = BarWindow("SPY", ONE_MINUTE, capacity=3)
    for offset in range(5):
        window.append(minute_bar(offset, str(100 + offset)))
    assert len(window) == 3
    assert window.is_full
    assert [float(b.close) for b in window.bars] == [102.0, 103.0, 104.0]
    assert list(window.closes()) == [102.0, 103.0, 104.0]


def test_window_arrays_are_read_only() -> None:
    """Handing out a writable view would let a strategy corrupt shared state."""
    window = BarWindow("SPY", ONE_MINUTE, capacity=3)
    window.append(minute_bar(0))
    with pytest.raises(ValueError, match="read-only"):
        window.closes()[0] = 999.0


def test_window_rejects_out_of_order_and_mismatched_bars() -> None:
    window = BarWindow("SPY", ONE_MINUTE, capacity=5)
    window.append(minute_bar(1))
    with pytest.raises(ValueError, match="not after"):
        window.append(minute_bar(0))
    with pytest.raises(ValueError, match="appended to SPY"):
        window.append(minute_bar(2, symbol="QQQ"))


def test_window_tracks_last_and_emptiness() -> None:
    window = BarWindow("SPY", ONE_MINUTE, capacity=2)
    assert window.last is None
    assert not window.is_full
    window.append(minute_bar(0, "101"))
    assert window.last is not None
    assert window.last.close == Decimal("101")


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        Bar(
            symbol="SPY",
            timeframe=ONE_MINUTE,
            timestamp=datetime(2026, 9, 3, 13, 30),  # noqa: DTZ001 - the point
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
        )
