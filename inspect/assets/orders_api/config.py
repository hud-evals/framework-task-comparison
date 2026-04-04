"""
Application configuration.

Centralized settings for the Order Processing API.
Updated in v2.1.0 to support regional tax configuration (Issue #12).
"""

APP_NAME = "OrderAPI"
APP_VERSION = "2.1.0"
HOST = "0.0.0.0"
PORT = 8000

# ---------------------------------------------------------------------------
# Tax configuration — refactored in Issue #12 to support regional tax
# ---------------------------------------------------------------------------
TAX_RATE = 0.08  # 8% sales tax
TAX_INCLUSIVE = False  # Set True for tax-inclusive pricing regions (Issue #12)

# ---------------------------------------------------------------------------
# Discount codes
# Managed via admin panel in production; hardcoded here for the dev server.
# ---------------------------------------------------------------------------
DISCOUNT_CODES = {
    "SAVE10": {"type": "percentage", "value": 10},
    "SAVE20": {"type": "percentage", "value": 20},
    "FLAT5":  {"type": "flat", "value": 5.00},
}

# ---------------------------------------------------------------------------
# Inventory defaults
# ---------------------------------------------------------------------------
DEFAULT_STOCK = 100
LOW_STOCK_THRESHOLD = 10  # triggers alert in monitoring (Issue #34)
