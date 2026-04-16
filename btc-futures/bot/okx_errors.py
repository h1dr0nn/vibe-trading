"""OKX API error classification and retry wrapper."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ── Custom exceptions ─────────────────────────────────────────────────────────

class OKXError(Exception):
    """Base OKX error."""


class RateLimitError(OKXError):
    def __init__(self, retry_after: float = 2.0):
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after}s")


class ServerError(OKXError):
    """OKX 5xx or transient server failure."""


class NetworkTimeoutError(OKXError):
    """Request timed out."""


class InvalidSignatureError(OKXError):
    """API key/signature rejected — fatal, bot must stop."""


class InsufficientMarginError(OKXError):
    """Account does not have enough margin."""


class OrderNotFoundError(OKXError):
    """Order no longer exists on OKX (already cancelled/filled)."""


class PositionNotFoundError(OKXError):
    """Position no longer exists on OKX."""


class ParameterError(OKXError):
    """Bad request parameters — indicates a bug in the calling code."""


class SkipCycleError(OKXError):
    """Raised when all retries are exhausted — caller should skip this cycle."""


# ── OKX error-code → exception mapping ───────────────────────────────────────

_OKX_CODE_MAP: dict[str, type[OKXError]] = {
    "50102": InvalidSignatureError,   # Invalid signature
    "50103": InvalidSignatureError,   # Invalid API key
    "50111": InvalidSignatureError,   # Invalid passphrase
    "51000": ParameterError,          # Parameter error
    "51008": InsufficientMarginError, # Insufficient margin
    "51010": OrderNotFoundError,      # Order not exist
    "51020": PositionNotFoundError,   # Position not exist
    "51400": OrderNotFoundError,      # Cancellation failed — order does not exist
}


def classify_okx_code(code: str, msg: str) -> OKXError:
    """Map an OKX API error code to the appropriate exception."""
    exc_class = _OKX_CODE_MAP.get(code, OKXError)
    return exc_class(f"OKX {code}: {msg}")


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    """Call fn with automatic retry for transient errors.

    Retryable: RateLimitError, ServerError, NetworkTimeoutError.
    Fatal (re-raises immediately): InvalidSignatureError, ParameterError.
    Exhausted retries: raises SkipCycleError.
    """
    import requests  # local import to avoid circular

    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)

        except InvalidSignatureError:
            logger.critical("Invalid API signature — stopping bot")
            raise

        except ParameterError as exc:
            logger.error("Parameter error (bug in caller): %s", exc)
            raise

        except InsufficientMarginError as exc:
            logger.error("Insufficient margin: %s", exc)
            raise

        except RateLimitError as exc:
            wait = exc.retry_after
            logger.warning("Rate limited — waiting %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
            time.sleep(wait)

        except ServerError as exc:
            wait = 2 ** attempt
            logger.warning("OKX server error — retry in %ds (attempt %d/%d): %s", wait, attempt + 1, max_retries, exc)
            time.sleep(wait)

        except (NetworkTimeoutError, requests.Timeout) as exc:
            wait = 2 ** attempt
            logger.warning("Timeout — retry in %ds (attempt %d/%d): %s", wait, attempt + 1, max_retries, exc)
            time.sleep(wait)

        except requests.ConnectionError as exc:
            wait = 2 ** attempt
            logger.warning("Connection error — retry in %ds (attempt %d/%d): %s", wait, attempt + 1, max_retries, exc)
            time.sleep(wait)

    logger.error("Exhausted %d retries for %s — skipping cycle", max_retries, fn.__name__)
    raise SkipCycleError(f"Exhausted {max_retries} retries for {fn.__name__}")
