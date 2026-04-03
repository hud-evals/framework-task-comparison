"""
Order Processing API — lightweight HTTP server.

Built on the stdlib ``http.server`` module so the project has zero
external dependencies.  Start with:

    python app.py

Endpoints
---------
GET  /api/products                  List catalogue
POST /api/orders                    Create an order
GET  /api/orders                    List all orders
GET  /api/orders/{id}               Get order by ID
POST /api/orders/{id}/apply-discount  Apply a discount code
"""

import json
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

import config
import database
from services.order_service import create_order, get_order, list_orders, apply_discount_to_order
from utils.validators import validate_discount_payload
from utils.money import format_price


# ---------------------------------------------------------------------------
# Route patterns
# ---------------------------------------------------------------------------
_ORDERS_LIST   = re.compile(r"^/api/orders/?$")
_ORDER_DETAIL  = re.compile(r"^/api/orders/([^/]+)/?$")
_ORDER_DISCOUNT = re.compile(r"^/api/orders/([^/]+)/apply-discount/?$")
_PRODUCTS_LIST = re.compile(r"^/api/products/?$")


class RequestHandler(BaseHTTPRequestHandler):
    """Minimal JSON API request handler."""

    # -- helpers ------------------------------------------------------------

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw)

    # -- GET ----------------------------------------------------------------

    def do_GET(self):
        if _PRODUCTS_LIST.match(self.path):
            products = [
                {
                    "id": p.id,
                    "name": p.name,
                    "price": p.price,
                    "display_price": format_price(p.price),
                    "category": p.category,
                }
                for p in database.list_products()
            ]
            return self._send_json({"products": products})

        if _ORDERS_LIST.match(self.path):
            return self._send_json({"orders": list_orders()})

        m = _ORDER_DETAIL.match(self.path)
        if m:
            order = get_order(m.group(1))
            if order is None:
                return self._send_json({"error": "Order not found"}, 404)
            return self._send_json(order)

        self._send_json({"error": "Not found"}, 404)

    # -- POST ---------------------------------------------------------------

    def do_POST(self):
        if _ORDERS_LIST.match(self.path):
            payload = self._read_body()
            result = create_order(payload)
            status = 400 if "error" in result else 201
            return self._send_json(result, status)

        m = _ORDER_DISCOUNT.match(self.path)
        if m:
            payload = self._read_body()
            errors = validate_discount_payload(payload)
            if errors:
                return self._send_json({"error": errors}, 400)

            result = apply_discount_to_order(m.group(1), payload["discount_code"])
            if result is None:
                return self._send_json({"error": "Order not found"}, 404)
            return self._send_json(result)

        self._send_json({"error": "Not found"}, 404)

    # Silence default stderr logging
    def log_message(self, fmt, *args):  # noqa: ARG002
        pass


def main():
    server = HTTPServer((config.HOST, config.PORT), RequestHandler)
    print(f"{config.APP_NAME} v{config.APP_VERSION} listening on "
          f"http://{config.HOST}:{config.PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
