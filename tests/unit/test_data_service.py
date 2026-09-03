"""MarketDataService: warm-up, quality routing, gap backfill, session close.

Driven by fakes, so the whole pipeline is exercised without a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradingbot.data.historical import Adjustment
from tradingbot.data.quality import QualityLimits
from tradingbot.data.recorder import BarRecorder
from tradingbot.data.service import MarketDataService
from tradingbot.data.stream import BarHandler
from tradingbot.data.types import ONE_MINUTE, Bar, TimeFrame, TimeFrameUnit

SESSION_OPEN = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
FIVE_MIN = TimeFrame(5, TimeFrameUnit.MINUTE)


def minute_bar(offset: int, close: str = "100", *, symbol: str = "SPY") -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timeframe=ONE_MINUTE,
        timestamp=SESSION_OPEN + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
        trade_count=1,
    )


class FakeHistorical:
    """Serves a canned set of bars and records what was asked for."""

    def __init__(self, bars: dict[str, list[Bar]] | None = None) -> None:
        self._bars = bars or {}
        self.calls: list[tuple[tuple[str, ...], str, datetime, datetime | None]] = []
        self.fail = False

    def fetch(
        self,
        symbols: Sequence[str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        self.calls.append((tuple(symbols), str(timeframe), start, end))
        if self.fail:
            msg = "historical unavailable"
            raise RuntimeError(msg)
        return {
            s: [b for b in self._bars.get(s, []) if b.timeframe == timeframe]
            for s in symbols
        }


class FakeStream:
    def __init__(self) -> None:
        self.handler: BarHandler | None = None
        self.symbols: list[str] = []
        self.running = False
        self.stopped = False

    def subscribe(self, symbols: Sequence[str], handler: BarHandler) -> None:
        self.symbols = list(symbols)
        self.handler = handler

    async def run(self) -> None:
        self.running = True
        await asyncio.Event().wait()  # blocks until cancelled

    async def stop(self) -> None:
        self.stopped = True


def build_service(
    tmp_path: Path,
    *,
    historical: FakeHistorical | None = None,
    timeframes: Sequence[TimeFrame] = (FIVE_MIN,),
    symbols: Sequence[str] = ("SPY",),
    sink: Any = None,
    **kwargs: Any,
) -> tuple[MarketDataService, FakeStream, FakeHistorical]:
    stream = FakeStream()
    hist = historical or FakeHistorical()
    service = MarketDataService(
        symbols,
        timeframes,
        stream=stream,
        historical=hist,
        recorder=BarRecorder(tmp_path, flush_threshold=1000),
        session_open=SESSION_OPEN,
        sink=sink,
        **kwargs,
    )
    return service, stream, hist


# --- warm-up ------------------------------------------------------------------


def test_warmup_loads_history_and_starts_the_watchdog(tmp_path: Path) -> None:
    history = {"SPY": [minute_bar(i) for i in range(3)]}
    service, _, hist = build_service(
        tmp_path, historical=FakeHistorical(history), timeframes=(ONE_MINUTE,)
    )
    loaded = service.warm_up(until=SESSION_OPEN + timedelta(hours=1))

    assert loaded == {"SPY": 3}
    assert hist.calls[0][0] == ("SPY",)
    assert service.last_sealed("SPY", ONE_MINUTE) is not None


def test_short_warmup_is_logged_not_fatal(tmp_path: Path) -> None:
    """One thin name must not stop the session."""
    service, _, _ = build_service(tmp_path, timeframes=(ONE_MINUTE,), warmup_bars=500)
    assert service.warm_up(until=SESSION_OPEN) == {"SPY": 0}


# --- quality routing ----------------------------------------------------------


async def test_rejected_bar_never_reaches_the_sink(tmp_path: Path) -> None:
    published: list[Bar] = []

    async def sink(bar: Bar) -> None:
        published.append(bar)

    service, _, _ = build_service(tmp_path, sink=sink, timeframes=(ONE_MINUTE,))
    await service.on_minute_bar(minute_bar(0))
    bad = Bar(
        symbol="SPY",
        timeframe=ONE_MINUTE,
        timestamp=SESSION_OPEN + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("90"),  # high below open: impossible
        low=Decimal("89"),
        close=Decimal("95"),
        volume=Decimal("1"),
    )
    await service.on_minute_bar(bad)

    assert service.stats.rejected == 1
    assert all(b.timestamp != bad.timestamp for b in published)


async def test_suspect_bar_is_published_but_flagged(tmp_path: Path) -> None:
    """A 30% move might be real. It is flagged so entries stop, not dropped."""
    published: list[Bar] = []

    async def sink(bar: Bar) -> None:
        published.append(bar)

    service, _, _ = build_service(
        tmp_path,
        sink=sink,
        timeframes=(ONE_MINUTE,),
        quality_limits=QualityLimits(max_bar_move_pct=Decimal("20")),
    )
    await service.on_minute_bar(minute_bar(0, "100"))
    await service.on_minute_bar(minute_bar(1, "150"))

    assert service.stats.suspect == 1
    assert published[-1].suspect


async def test_unknown_symbol_is_ignored(tmp_path: Path) -> None:
    service, _, _ = build_service(tmp_path)
    await service.on_minute_bar(minute_bar(0, symbol="NVDA"))
    assert service.stats.received == 0


# --- gap backfill -------------------------------------------------------------


async def test_gap_triggers_backfill_and_fills_the_window(tmp_path: Path) -> None:
    """The core Phase 1 property: a hole in the live series is filled from the
    historical API so indicator windows stay continuous."""
    published: list[Bar] = []

    async def sink(bar: Bar) -> None:
        published.append(bar)

    missing = [minute_bar(i, "10%d" % i) for i in (1, 2, 3)]
    service, _, hist = build_service(
        tmp_path,
        historical=FakeHistorical({"SPY": missing}),
        timeframes=(ONE_MINUTE,),
        sink=sink,
    )

    await service.on_minute_bar(minute_bar(0, "100"))
    await service.on_minute_bar(minute_bar(4, "104"))

    assert service.stats.gaps_detected == 1
    assert service.stats.backfilled == 3
    # 0 live, 1-3 backfilled, then 4 live: a contiguous series with no hole.
    assert [b.timestamp for b in published] == [
        SESSION_OPEN + timedelta(minutes=i) for i in range(5)
    ]


async def test_backfill_failure_is_survivable(tmp_path: Path) -> None:
    hist = FakeHistorical({"SPY": [minute_bar(1)]})
    service, _, _ = build_service(
        tmp_path, historical=hist, timeframes=(ONE_MINUTE,)
    )
    hist.fail = True
    await service.on_minute_bar(minute_bar(0))
    await service.on_minute_bar(minute_bar(5))
    assert service.stats.gaps_detected == 1
    assert service.stats.backfilled == 0


async def test_contiguous_bars_do_not_trigger_backfill(tmp_path: Path) -> None:
    service, _, hist = build_service(tmp_path, timeframes=(ONE_MINUTE,))
    for i in range(4):
        await service.on_minute_bar(minute_bar(i))
    assert service.stats.gaps_detected == 0
    assert hist.calls == []


# --- staleness ----------------------------------------------------------------


async def test_stale_symbols_are_reported(tmp_path: Path) -> None:
    service, _, _ = build_service(
        tmp_path, symbols=("SPY", "QQQ"), stale_threshold=timedelta(seconds=1)
    )
    service.warm_up(until=datetime.now(UTC) - timedelta(seconds=10))
    assert set(service.stale_symbols()) == {"SPY", "QQQ"}


# --- session lifecycle --------------------------------------------------------


async def test_close_session_seals_and_flushes(tmp_path: Path) -> None:
    service, _, _ = build_service(tmp_path)
    for i in range(3):
        await service.on_minute_bar(minute_bar(i))
    assert service.stats.published == 0  # 5Min bar not sealed yet

    service.close_session()
    assert service.stats.published == 1
    assert service.verify_continuity("SPY", FIVE_MIN) == []


async def test_recorded_series_is_gap_free(tmp_path: Path) -> None:
    """The Phase 1 exit criterion, asserted directly."""
    service, _, _ = build_service(tmp_path, timeframes=(ONE_MINUTE,))
    for i in range(10):
        await service.on_minute_bar(minute_bar(i))
    service.close_session()
    assert service.verify_continuity("SPY", ONE_MINUTE) == []


async def test_run_subscribes_and_stops_cleanly(tmp_path: Path) -> None:
    service, stream, _ = build_service(tmp_path)
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    assert stream.symbols == ["SPY"]
    assert stream.handler is not None

    await service.stop()
    await asyncio.wait_for(task, timeout=2)
    assert stream.stopped


@pytest.mark.parametrize("symbol", ["spy", "SPY"])
async def test_symbols_are_normalised(tmp_path: Path, symbol: str) -> None:
    service, _, _ = build_service(tmp_path, symbols=(symbol,), timeframes=(ONE_MINUTE,))
    await service.on_minute_bar(minute_bar(0))
    assert service.stats.received == 1
