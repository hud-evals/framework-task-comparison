"""
Pricing engine for the Order Processing API.

Handles subtotal computation, tax calculation, and discount application.
Refactored in v2.0 to use the shared ``round_cents`` helper so that all
monetary arithmetic is consistent across the codebase (Issue #19).
"""

import config
from utils.money import round_cents


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_order_total(items: list[dict], discount_code: str | None = None) -> dict:
    """Return a pricing breakdown for the given line-items.

    Parameters
    ----------
    items : list[dict]
        Each dict must contain ``price`` (float) and ``quantity`` (int).
    discount_code : str | None
        Optional promotional code to apply.

    Returns
    -------
    dict with keys: subtotal, discount, tax, total
    """

    # 1. Line-item subtotal
    subtotal = sum(item["price"] * item["quantity"] for item in items)

    # 2. Calculate tax on order
    tax = round_cents(subtotal * config.TAX_RATE)

    # 3. Apply discount (if any)
    discount_amount = 0.0
    if discount_code:
        discount_amount = apply_discount(subtotal, discount_code)

    # 4. Final total: subtotal + tax - discount
    total = round_cents(subtotal + tax - discount_amount)

    return {
        "subtotal": round_cents(subtotal),
        "discount": round_cents(discount_amount),
        "tax": tax,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Discount helpers
# ---------------------------------------------------------------------------

def apply_discount(subtotal: float, code: str) -> float:
    """Look up *code* and compute the discount amount against *subtotal*."""
    code = code.strip().upper()
    entry = config.DISCOUNT_CODES.get(code)
    if entry is None:
        return 0.0

    if entry["type"] == "percentage":
        return round_cents(subtotal * entry["value"] / 100)

    if entry["type"] == "flat":
        # Never discount more than the subtotal
        return round_cents(min(entry["value"], subtotal))

    return 0.0


def get_available_discounts() -> list[dict]:
    """Return a summary of all active discount codes (admin use)."""
    out = []
    for code, info in config.DISCOUNT_CODES.items():
        out.append({"code": code, "type": info["type"], "value": info["value"]})
    return out
