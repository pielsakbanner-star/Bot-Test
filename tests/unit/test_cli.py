"""CLI surface: exit codes and the live-mode refusals.

Exit codes are part of the contract -- the runbook and any supervisor script
branch on them:

    0  all checks passed
    1  a check failed
    2  config error or refusal
    3  command not implemented yet
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import MINIMAL_CONFIG
from tradingbot import __version__
from tradingbot.__main__ import app

runner = CliRunner()


def output(result: Any) -> str:
    """stdout + stderr, whichever way the installed click splits them."""
    text = result.output or ""
    with contextlib.suppress(ValueError, AttributeError):
        text += result.stderr or ""
    return text


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real .env or keys leak into these tests."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_LIVE_KEY_ID",
        "ALPACA_LIVE_SECRET_KEY",
        "ALERT_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def write(tmp_path: Path, name: str = "paper.yaml", **overrides: Any) -> Path:
    data = {**MINIMAL_CONFIG, **overrides}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in output(result)


def test_no_args_shows_help() -> None:
    assert runner.invoke(app, []).exit_code != 0


def test_missing_config_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2


def test_missing_credentials_is_a_config_error(tmp_path: Path) -> None:
    path = write(tmp_path)
    result = runner.invoke(app, ["doctor", "--config", str(path)])
    assert result.exit_code == 2
    assert "ALPACA_PAPER" in output(result)


def test_live_config_without_acknowledgement_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, name="live.yaml", mode="live")
    result = runner.invoke(app, ["doctor", "--config", str(path)])
    assert result.exit_code == 2
    assert "REFUSED" in output(result)
    assert "i-understand-the-risk" in output(result)


def test_live_mode_flag_cannot_override_a_paper_config(tmp_path: Path) -> None:
    path = write(tmp_path)
    result = runner.invoke(
        app,
        ["doctor", "--config", str(path), "--mode", "live", "--i-understand-the-risk"],
    )
    assert result.exit_code == 2
    assert "contradicts" in output(result)


def test_run_is_not_implemented_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "PKID000000")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "paper-secret-0123456789")
    path = write(tmp_path)
    result = runner.invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 3
    assert "Phase 3" in output(result)
