"""Tests for the order pricing engine.

Validates that order totals are calculated correctly
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from services.pricing_service import calculate_order_total


class TestOrderPricing:
    """Test order total calculations with various discount scenarios."""

    def test_basic_order_no_discount(self):
        """Order without discount should have correct subtotal + tax."""
        items = [{"price": 25.00, "quantity": 2}]
        result = calculate_order_total(items)

        assert result["subtotal"] == 50.00
        assert result["discount"] == 0.00
        assert result["tax"] == 4.00  # 50 * 0.08
        assert result["total"] == 54.00

    def test_percentage_discount_order_total(self):
        """10% discount on $50 with 8% tax should produce $48.60 total."""
        items = [{"price": 25.00, "quantity": 2}]
        result = calculate_order_total(items, discount_code="SAVE10")

        # Correct: ($50 - $5) * 1.08 = $48.60
        assert result["subtotal"] == 50.00
        assert result["discount"] == 5.00
        assert result["tax"] == 3.60  # tax on discounted $45
        assert result["total"] == 48.60

    def test_twenty_percent_discount(self):
        """20% discount on $49.99 item should round correctly."""
        items = [{"price": 49.99, "quantity": 1}]
        result = calculate_order_total(items, discount_code="SAVE20")

        # 20% of $49.99 = $10.00 (rounded)
        # Discounted: $49.99 - $10.00 = $39.99
        # Tax: $39.99 * 0.08 = $3.20
        # Total: $39.99 + $3.20 = $43.19
        assert result["discount"] == 10.00, (
            f"20% of $49.99 should be $10.00, got ${result['discount']:.2f}"
        )
        assert result["total"] == 43.19, (
            f"Expected total $43.19, got ${result['total']:.2f}"
        )

    def test_flat_discount(self):
        """Flat $5 discount should work correctly."""
        items = [{"price": 25.00, "quantity": 2}]
        result = calculate_order_total(items, discount_code="FLAT5")

        # $50 - $5 = $45, tax = $45 * 0.08 = $3.60, total = $48.60
        assert result["discount"] == 5.00
        assert result["tax"] == 3.60
        assert result["total"] == 48.60

    def test_multi_item_discounted_order(self):
        """Multiple different items with a discount."""
        items = [
            {"price": 25.00, "quantity": 1},   # Widget
            {"price": 49.99, "quantity": 1},   # Gadget
            {"price": 12.50, "quantity": 2},   # 2x Doohickey
        ]
        result = calculate_order_total(items, discount_code="SAVE10")

        # Subtotal: 25 + 49.99 + 25 = 99.99
        # Discount: 10% of 99.99 = 10.00 (rounded)
        # Discounted: 99.99 - 10.00 = 89.99
        # Tax: 89.99 * 0.08 = 7.20
        # Total: 89.99 + 7.20 = 97.19
        assert result["subtotal"] == 99.99
        assert result["discount"] == 10.00, (
            f"10% of $99.99 should be $10.00, got ${result['discount']:.2f}"
        )
        assert result["total"] == 97.19, (
            f"Expected total $97.19, got ${result['total']:.2f}"
        )
