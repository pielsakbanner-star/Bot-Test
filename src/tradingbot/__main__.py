"""CLI entrypoint.

Phase 0 ships `doctor` and `version`. `run` exists so that reaching for it
gives an honest answer rather than "no such command"; it refuses to start until
the execution layer lands in Phase 3.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from tradingbot import __version__
from tradingbot.broker.alpaca import AlpacaBroker, AlpacaFeedProbe
from tradingbot.config import AppConfig, Mode, Secrets, load_config
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
