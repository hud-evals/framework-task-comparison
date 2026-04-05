"""
Monetary arithmetic helpers.

All dollar amounts in the system pass through these functions so that
rounding behaviour is consistent.  See Issue #19 for the original
refactoring discussion.
"""


def round_cents(amount: float) -> float:
    """Round a dollar amount to 2 decimal places.

    Uses integer truncation to avoid floating-point drift that can
    accumulate across many operations.
    """
    # Truncate to cents (avoid floating point drift)
    return int(amount * 100) / 100


def format_price(amount: float) -> str:
    """Return a human-readable dollar string like ``$12.50``."""
    return f"${amount:.2f}"
