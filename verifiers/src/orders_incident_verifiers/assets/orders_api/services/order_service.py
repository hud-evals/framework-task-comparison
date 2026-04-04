"""
Order orchestration service.

Coordinates validation, inventory checks, pricing, and persistence
for the order lifecycle.
"""

from models import Order, OrderItem
import database
from services.pricing_service import calculate_order_total
from services.inventory_service import check_availability, reserve_stock
from utils.validators import validate_order_payload


def create_order(payload: dict) -> dict:
    """Validate input, price the order, persist it, and return a summary."""

    errors = validate_order_payload(payload)
    if errors:
        return {"error": errors}

    raw_items = payload["items"]
    discount_code = payload.get("discount_code")

    # Build typed line-items and verify stock
    order_items: list[OrderItem] = []
    pricing_items: list[dict] = []

    for entry in raw_items:
        product = database.get_product(entry["product_id"])
        if product is None:
            return {"error": f"Unknown product: {entry['product_id']}"}

        qty = int(entry["quantity"])
        if not check_availability(product.id, qty):
            return {"error": f"Insufficient stock for {product.name}"}

        order_items.append(
            OrderItem(
                product_id=product.id,
                name=product.name,
                price=product.price,
                quantity=qty,
            )
        )
        pricing_items.append({"price": product.price, "quantity": qty})

    # Calculate totals
    totals = calculate_order_total(pricing_items, discount_code)

    # Reserve inventory
    for item in order_items:
        reserve_stock(item.product_id, item.quantity)

    # Persist
    order = Order(
        items=order_items,
        discount_code=discount_code,
        subtotal=totals["subtotal"],
        discount=totals["discount"],
        tax=totals["tax"],
        total=totals["total"],
        status="confirmed",
    )
    database.save_order(order)

    return _order_to_dict(order)


def get_order(order_id: str) -> dict | None:
    order = database.get_order(order_id)
    if order is None:
        return None
    return _order_to_dict(order)


def list_orders() -> list[dict]:
    return [_order_to_dict(o) for o in database.list_orders()]


def apply_discount_to_order(order_id: str, discount_code: str) -> dict | None:
    """Re-price an existing order with a discount code."""
    order = database.get_order(order_id)
    if order is None:
        return None

    pricing_items = [
        {"price": it.price, "quantity": it.quantity} for it in order.items
    ]
    totals = calculate_order_total(pricing_items, discount_code)

    order.discount_code = discount_code
    order.subtotal = totals["subtotal"]
    order.discount = totals["discount"]
    order.tax = totals["tax"]
    order.total = totals["total"]
    database.save_order(order)

    return _order_to_dict(order)


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _order_to_dict(order: Order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "items": [
            {
                "product_id": it.product_id,
                "name": it.name,
                "price": it.price,
                "quantity": it.quantity,
            }
            for it in order.items
        ],
        "discount_code": order.discount_code,
        "subtotal": order.subtotal,
        "discount": order.discount,
        "tax": order.tax,
        "total": order.total,
        "created_at": order.created_at,
    }
