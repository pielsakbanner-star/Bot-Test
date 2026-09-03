"""Parquet recording round trips.

The property that matters: what goes in comes back out *exactly*. A recording
that quietly rounds prices is worse than no recording, because you would trust
it while debugging a divergence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradingbot.data.historical import bars_between, detect_gaps, merge_warmup
from tradingbot.data.recorder import BarRecorder, bars_to_table, table_to_bars
from tradingbot.data.types import ONE_MINUTE, Bar, TimeFrame, TimeFrameUnit

T0 = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
FIVE_MIN = TimeFrame(5, TimeFrameUnit.MINUTE)


def bar(minute: int = 0, close: str = "100.05", *, symbol: str = "SPY", **kw: object) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timeframe=ONE_MINUTE,
        timestamp=T0 + timedelta(minutes=minute),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1234.5"),
        trade_count=7,
        vwap=Decimal("100.055"),
        **kw,  # type: ignore[arg-type]
    )


# --- exactness ----------------------------------------------------------------


def test_round_trip_preserves_decimals_exactly() -> None:
    """Stored as strings precisely so this holds. A float column would return
    100.04999999999999 and quietly poison every replay."""
    original = bar(close="100.05")
    (restored,) = table_to_bars(bars_to_table([original]))
    assert restored == original
    assert restored.close == Decimal("100.05")
    assert str(restored.close) == "100.05"


def test_round_trip_preserves_flags_and_nulls() -> None:
    original = Bar(
        symbol="SPY",
        timeframe=FIVE_MIN,
        timestamp=T0,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("0"),
        vwap=None,
        synthetic=True,
        suspect=True,
    )
    (restored,) = table_to_bars(bars_to_table([original]))
    assert restored.vwap is None
    assert restored.synthetic
    assert restored.suspect
    assert restored.timeframe == FIVE_MIN


# --- partitioning -------------------------------------------------------------


def test_writes_hive_partitioned_paths(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path, flush_threshold=1000)
    recorder.record([bar(0), bar(1, symbol="QQQ")])
    written = recorder.flush()

    assert len(written) == 2
    assert (
        tmp_path / "1Min" / "symbol=SPY" / "date=2026-09-03" / "bars.parquet"
    ).exists()
    assert (
        tmp_path / "1Min" / "symbol=QQQ" / "date=2026-09-03" / "bars.parquet"
    ).exists()


def test_slashed_crypto_symbols_do_not_nest_directories(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path)
    path = recorder.partition_path("BTC/USD", "1Min", T0.date())
    assert "symbol=BTC_USD" in str(path)
    assert path.parent.parent.name == "symbol=BTC_USD"


def test_flush_threshold_triggers_automatically(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path, flush_threshold=3)
    recorder.record([bar(0), bar(1)])
    assert recorder.buffered == 2
    recorder.record([bar(2)])
    assert recorder.buffered == 0
    assert recorder.written == 3


def test_appending_to_an_existing_partition_keeps_both_batches(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path, flush_threshold=1000)
    recorder.record([bar(0)])
    recorder.flush()
    recorder.record([bar(1)])
    recorder.flush()

    restored = recorder.read("SPY", ONE_MINUTE, T0)
    assert [b.timestamp for b in restored] == [T0, T0 + timedelta(minutes=1)]


def test_reading_a_missing_partition_is_empty_not_an_error(tmp_path: Path) -> None:
    assert BarRecorder(tmp_path).read("NOPE", ONE_MINUTE, T0) == []


def test_disabled_recorder_writes_nothing(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path, enabled=False)
    recorder.record([bar(0)])
    assert recorder.flush() == []
    assert recorder.buffered == 0
    assert not any(tmp_path.iterdir())


def test_flush_with_nothing_buffered_is_a_no_op(tmp_path: Path) -> None:
    assert BarRecorder(tmp_path).flush() == []


# --- gap analysis -------------------------------------------------------------


def test_detect_gaps_finds_the_hole() -> None:
    bars = [bar(0), bar(1), bar(5)]
    assert detect_gaps(bars, ONE_MINUTE) == [
        (T0 + timedelta(minutes=2), T0 + timedelta(minutes=5))
    ]


def test_a_contiguous_series_has_no_gaps() -> None:
    assert detect_gaps([bar(i) for i in range(5)], ONE_MINUTE) == []


def test_gap_detection_on_a_short_series() -> None:
    assert detect_gaps([], ONE_MINUTE) == []
    assert detect_gaps([bar(0)], ONE_MINUTE) == []


def test_bars_between_counts_intervals() -> None:
    assert bars_between(T0, T0 + timedelta(minutes=5), ONE_MINUTE) == 5
    assert bars_between(T0, T0, ONE_MINUTE) == 0
    assert bars_between(T0 + timedelta(minutes=5), T0, ONE_MINUTE) == 0


# --- warm-up merging ----------------------------------------------------------


def test_merge_prefers_live_bars_on_overlap() -> None:
    """A reconnect backfill overlaps what we already hold. The live bar is what
    the strategy actually saw, so it wins."""
    history = [bar(0, "100"), bar(1, "101")]
    live = [bar(1, "199"), bar(2, "102")]
    merged = merge_warmup(history, live)
    assert [str(b.close) for b in merged] == ["100", "199", "102"]


def test_merge_respects_capacity() -> None:
    history = [bar(i, str(100 + i)) for i in range(10)]
    merged = merge_warmup(history, [], capacity=3)
    assert len(merged) == 3
    assert [str(b.close) for b in merged] == ["107", "108", "109"]


def test_merge_sorts_by_timestamp() -> None:
    merged = merge_warmup([bar(3)], [bar(1), bar(2)])
    assert [b.timestamp for b in merged] == [
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
        T0 + timedelta(minutes=3),
    ]
