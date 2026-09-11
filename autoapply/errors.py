"""Typed exception taxonomy for the whole system.

The seam rules: pipeline stages raise (never swallow) these, and the
CLI/logging layer maps them to messages. Later the captcha-loop, Telegram
courier, and agent supervisor hook into these exact types.
"""


class AutoApplyError(Exception):
    """Base class for all expected pipeline failures."""


class ConfigError(AutoApplyError):
    """Bad, missing, or contradictory configuration."""


class LoginRequired(AutoApplyError):
    """The source session is not authenticated."""


class AccountBlocked(AutoApplyError):
    """The source presented a block/guardrail page (checkpoint, temp limit, hard block)."""


class CaptchaDetected(AutoApplyError):
    """A CAPTCHA challenge is in the way. V1: pause + surface to user."""


class RateLimited(AutoApplyError):
    """The source throttled us (HTTP 429 or equivalent)."""


class BudgetExceeded(AutoApplyError):
    """A configured per-run or per-day budget is exhausted."""


class ParseError(AutoApplyError):
    """The page structure could not be parsed (page changed, selector stale)."""
