"""
Inventory service.

Checks product availability before an order is confirmed.
Currently backed by static stock levels seeded in ``database.py``;
planned integration with the warehouse API is tracked in Issue #34.
"""

import config
import database


# In-memory stock ledger — seeded once on import
_stock: dict[str, int] = {}


def _seed_stock() -> None:
    for product in database.list_products():
        _stock[product.id] = config.DEFAULT_STOCK

_seed_stock()


def check_availability(product_id: str, quantity: int) -> bool:
    """Return True if *quantity* units of *product_id* are in stock."""
    # TODO: connect to real inventory system (Issue #34)
    stock = _stock.get(product_id.upper(), 0)
    return stock >= quantity


def reserve_stock(product_id: str, quantity: int) -> bool:
    """Decrement stock for a confirmed order line-item."""
    pid = product_id.upper()
    if not check_availability(pid, quantity):
        return False
    _stock[pid] -= quantity
    return True


def get_stock_level(product_id: str) -> int:
    return _stock.get(product_id.upper(), 0)
