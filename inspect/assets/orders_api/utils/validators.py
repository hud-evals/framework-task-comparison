"""
Input validation for API payloads.

Each ``validate_*`` function returns a list of human-readable error
strings.  An empty list means the payload is valid.
"""

import config


def validate_order_payload(payload: dict) -> list[str]:
    """Validate a create-order request body."""
    errors: list[str] = []

    if not isinstance(payload, dict):
        return ["Request body must be a JSON object."]

    items = payload.get("items")
    if not items or not isinstance(items, list):
        errors.append("'items' must be a non-empty list.")
        return errors

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Item at index {idx} must be an object.")
            continue

        if "product_id" not in item:
            errors.append(f"Item {idx}: 'product_id' is required.")

        qty = item.get("quantity")
        if qty is None:
            errors.append(f"Item {idx}: 'quantity' is required.")
        elif not isinstance(qty, int) or qty < 1:
            errors.append(f"Item {idx}: 'quantity' must be a positive integer.")

    # Validate discount code format (if supplied)
    code = payload.get("discount_code")
    if code is not None:
        if not isinstance(code, str) or len(code.strip()) == 0:
            errors.append("'discount_code' must be a non-empty string.")
        elif code.strip().upper() not in config.DISCOUNT_CODES:
            errors.append(f"Unknown discount code: '{code}'.")

    return errors


def validate_discount_payload(payload: dict) -> list[str]:
    """Validate an apply-discount request body."""
    errors: list[str] = []

    code = payload.get("discount_code")
    if code is None or not isinstance(code, str) or len(code.strip()) == 0:
        errors.append("'discount_code' is required.")
    elif code.strip().upper() not in config.DISCOUNT_CODES:
        errors.append(f"Unknown discount code: '{code}'.")

    return errors
