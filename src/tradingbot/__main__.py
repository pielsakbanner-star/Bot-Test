"""CLI entrypoint.

Phase 0 ships `doctor` and `version`. `run` exists so that reaching for it
gives an honest answer rather than "no such command"; it refuses to start until
the execution layer lands in Phase 3.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from tradingbot import __version__
from tradingbot.broker.alpaca import AlpacaBroker, AlpacaFeedProbe
from tradingbot.config import AppConfig, AssetClass, Mode, Secrets, load_config
from tradingbot.data.historical import AlpacaHistoricalBars
from tradingbot.data.quality import QualityLimits
from tradingbot.data.recorder import BarRecorder
from tradingbot.data.service import MarketDataService
from tradingbot.data.stream import AlpacaBarStream
from tradingbot.data.types import ONE_MINUTE, TimeFrame, utc_now
from tradingbot.doctor import Doctor
from tradingbot.errors import ConfigError, TradingBotError, UnsafeConfigError
from tradingbot.observability.logging import configure_logging, get_logger

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Automated trading bot for the Alpaca brokerage API.",
)

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to the YAML config file.",
    ),
]
ModeOption = Annotated[
    Mode | None,
    typer.Option(
        "--mode",
        help="Cross-check against the mode in the config file. Cannot override it.",
    ),
]
LiveAckOption = Annotated[
    bool,
    typer.Option(
        "--i-understand-the-risk",
        help="Required acknowledgement for live mode. Places real orders.",
    ),
]


def _load(config: Path, mode: Mode | None, live_ack: bool) -> tuple[AppConfig, Secrets]:
    """Load config and secrets, turning our errors into clean CLI failures."""
    try:
        app_config = load_config(config, live_ack=live_ack, mode_override=mode)
        secrets = Secrets()
        secrets.credentials_for(app_config.mode)
    except UnsafeConfigError as exc:
        typer.secho(f"REFUSED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except ConfigError as exc:
        typer.secho(f"CONFIG ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    return app_config, secrets


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def doctor(
    config: ConfigOption,
    mode: ModeOption = None,
    i_understand_the_risk: LiveAckOption = False,
) -> None:
    """Validate config, credentials, account, market state, and symbols.

    Places no orders and mutates nothing. Exits non-zero if any check fails.
    """
    cfg, secrets = _load(config, mode, i_understand_the_risk)

    key, secret = secrets.credentials_for(cfg.mode)
    configure_logging(
        cfg.observability,
        mode=cfg.mode,
        secrets=[key, secret, secrets.alert_webhook_url],
        quiet_console=True,
    )
    log = get_logger(__name__)

    broker = AlpacaBroker.from_secrets(secrets, cfg.mode)
    probe = AlpacaFeedProbe.from_secrets(secrets, cfg.mode)

    report = Doctor(cfg, secrets, broker, config_path=config, feed_probe=probe).run()

    typer.echo(report.render())
    log.info(
        "doctor.complete",
        checks=len(report.results),
        failures=len(report.failed),
        warnings=len(report.warned),
    )
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def stream(
    config: ConfigOption,
    minutes: Annotated[
        int,
        typer.Option(
            "--minutes",
            min=1,
            help="Stop after this many minutes. Omit --minutes to run until Ctrl-C.",
        ),
    ] = 0,
    mode: ModeOption = None,
) -> None:
    """Run the read-only market-data pipeline: warm up, stream, record.

    Places no orders. This is the Phase 1 deliverable -- use it to confirm the
    universe produces a gap-free recorded series before wiring up strategies.
    """
    cfg, secrets = _load(config, mode, live_ack=False)
    key, secret = secrets.credentials_for(cfg.mode)
    configure_logging(
        cfg.observability,
        mode=cfg.mode,
        secrets=[key, secret, secrets.alert_webhook_url],
    )
    log = get_logger(__name__)

    broker = AlpacaBroker.from_secrets(secrets, cfg.mode)
    session_open, session_close = _session_bounds(broker, cfg)

    timeframes = sorted(
        {ONE_MINUTE} | {TimeFrame.parse(s.timeframe) for s in cfg.enabled_strategies}
    )
    service = MarketDataService(
        cfg.universe.symbols,
        timeframes,
        stream=AlpacaBarStream.from_secrets(
            secrets,
            cfg.mode,
            asset_class=cfg.account.asset_class,
            feed=cfg.account.data_feed,
        ),
        historical=AlpacaHistoricalBars.from_secrets(
            secrets,
            cfg.mode,
            asset_class=cfg.account.asset_class,
            feed=cfg.account.data_feed,
        ),
        recorder=BarRecorder(cfg.storage.bars_dir, enabled=cfg.data.record_live_bars),
        session_open=session_open,
        session_close=session_close,
        quality_limits=QualityLimits(max_bar_move_pct=cfg.data.max_bar_move_pct),
        stale_threshold=timedelta(seconds=cfg.data.stale_threshold_seconds),
        warmup_multiplier=cfg.session.warmup_multiplier,
    )

    typer.echo(
        f"Warming up {len(cfg.universe.symbols)} symbols on "
        f"{', '.join(str(t) for t in timeframes)}..."
    )
    service.warm_up()
    typer.echo(
        "Streaming. Ctrl-C to stop."
        if not minutes
        else f"Streaming for {minutes} minute(s). Ctrl-C to stop early."
    )
    asyncio.run(_stream_until_stopped(service, minutes))

    stats = service.stats.as_dict()
    for name, value in stats.items():
        typer.echo(f"{name:>14}: {value}")
    for symbol in cfg.universe.symbols:
        for timeframe in timeframes:
            gaps = service.verify_continuity(symbol, timeframe)
            if gaps:
                typer.secho(
                    f"GAP {symbol} {timeframe}: {len(gaps)} hole(s)",
                    fg=typer.colors.RED,
                )
    log.info("stream.complete", **stats)


async def _stream_until_stopped(service: MarketDataService, minutes: int) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(service.run())

    pending: set[asyncio.Task[None]] = set()

    def request_stop() -> None:
        # Hold a reference: a bare create_task can be collected before it runs.
        stop_task = loop.create_task(service.stop())
        pending.add(stop_task)
        stop_task.add_done_callback(pending.discard)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, request_stop)

    if minutes:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=minutes * 60)
            return
        await service.stop()
    try:
        await task
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        await service.stop()
        await task


def _session_bounds(
    broker: AlpacaBroker, cfg: AppConfig
) -> tuple[datetime, datetime | None]:
    """Session open/close for bar alignment.

    Crypto has no calendar, so the aggregator is anchored to midnight UTC and
    the day rolls over there.
    """
    if cfg.account.asset_class is AssetClass.CRYPTO:
        start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    today = broker.get_clock().timestamp.date()
    days = broker.get_calendar(today, today)
    if not days:
        typer.secho(
            "Market is closed today; nothing to stream.", fg=typer.colors.YELLOW
        )
        raise typer.Exit(code=0)
    return days[0].open_at, days[0].close_at


@app.command()
def run(
    config: ConfigOption,
    mode: ModeOption = None,
    i_understand_the_risk: LiveAckOption = False,
) -> None:
    """Run the trading engine. Not implemented until Phase 3."""
    _load(config, mode, i_understand_the_risk)
    typer.secho(
        "The engine is not implemented yet.\n"
        "Phase 0 (config, logging, doctor) is complete; execution lands in "
        "Phase 3. See docs/roadmap.md.\n"
        "Config and credentials validated successfully -- run `doctor` for the "
        "full pre-flight check.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=3)


def main() -> None:
    try:
        app()
    except TradingBotError as exc:  # pragma: no cover - last-resort net
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
