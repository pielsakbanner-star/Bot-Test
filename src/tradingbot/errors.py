"""Exception hierarchy.

The broker adapter translates SDK and HTTP errors into this small set so that
nothing above ``broker/`` needs to know what an HTTP status code is. See
docs/alpaca-integration.md section 9.
"""

from __future__ import annotations


class TradingBotError(Exception):
    """Base for every error this package raises."""


# --- Configuration -----------------------------------------------------------


class ConfigError(TradingBotError):
    """Configuration is missing, malformed, or internally inconsistent."""


class UnsafeConfigError(ConfigError):
    """Configuration is valid but would be unsafe to run.

    Raised for the guards in docs/configuration.md section 3 -- live mode
    without the explicit CLI flag, live and paper sharing a config path, and so
    on. Separate from ConfigError so the CLI can present it differently: this is
    never a typo, it is always a decision the operator has to make deliberately.
    """


# --- Broker ------------------------------------------------------------------


class BrokerError(TradingBotError):
    """Base for broker-adapter failures."""


class AuthError(BrokerError):
    """Credentials were rejected. Fatal; the bot halts."""


class RateLimitedError(BrokerError):
    """HTTP 429. Retried with backoff by the adapter."""


class InsufficientBuyingPowerError(BrokerError):
    """The account cannot fund this order. The intent is rejected, not retried."""


class NotTradableError(BrokerError):
    """Asset is inactive or not tradable. The symbol is dropped for the session."""


class WashTradeBlockedError(BrokerError):
    """Self-crossing order rejected. Cancel the opposing order and retry once."""


class TransientBrokerError(BrokerError):
    """5xx or a timeout. Retried with backoff, bounded attempts."""


class PermanentOrderRejectError(BrokerError):
    """Any other rejection. Logged and alerted; never blind-retried."""


# --- Engine ------------------------------------------------------------------


class ReconciliationMismatchError(TradingBotError):
    """Local state disagreed with the broker. Broker state wins; this alerts."""


class KillSwitchEngagedError(TradingBotError):
    """The kill switch is set. Every intent is rejected while this holds."""
