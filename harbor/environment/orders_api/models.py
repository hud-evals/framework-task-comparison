"""
Domain models for the Order Processing API.

All models are plain dataclasses — no ORM dependency so the test-suite
can run without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Product:
    id: str
    name: str
    price: float
    category: str = "general"


@dataclass
class OrderItem:
    product_id: str
    name: str
    price: float
    quantity: int


@dataclass
class Order:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    items: List[OrderItem] = field(default_factory=list)
    discount_code: Optional[str] = None
    subtotal: float = 0.0
    discount: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
