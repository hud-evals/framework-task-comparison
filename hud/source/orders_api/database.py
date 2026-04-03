"""
In-memory data store.

Provides dict-backed storage for products and orders.  Replaced by
PostgreSQL in production (see Issue #8); this module is kept for local
development and integration tests.
"""

from models import Product

# ---- product catalogue ----------------------------------------------------

PRODUCTS: dict[str, Product] = {}

def _seed_products() -> None:
    """Load the default product catalogue."""
    defaults = [
        Product(id="WIDGET",    name="Widget",    price=25.00, category="parts"),
        Product(id="GADGET",    name="Gadget",    price=49.99, category="electronics"),
        Product(id="DOOHICKEY", name="Doohickey", price=12.50, category="parts"),
    ]
    for p in defaults:
        PRODUCTS[p.id] = p

_seed_products()

# ---- order storage --------------------------------------------------------

ORDERS: dict[str, "Order"] = {}  # type: ignore[name-defined]


def get_product(product_id: str) -> Product | None:
    return PRODUCTS.get(product_id.upper())


def list_products() -> list[Product]:
    return list(PRODUCTS.values())


def save_order(order) -> None:
    ORDERS[order.id] = order


def get_order(order_id: str):
    return ORDERS.get(order_id)


def list_orders() -> list:
    return list(ORDERS.values())
