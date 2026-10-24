"""Order pricing.

The buggy_deploy fault switches on a "new release" of this module that looks up a
promotion rate that does not exist for one product. That is a realistic bug: it only
breaks one code path, so the error rate rises but the service stays up.
"""

from decimal import ROUND_HALF_UP, Decimal

PROMOTION_RATES: dict[int, Decimal] = {1: Decimal("0.05"), 3: Decimal("0.10")}


def calculate_total(price: Decimal, quantity: int) -> Decimal:
    return (price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_total_v2(product_id: int, price: Decimal, quantity: int) -> Decimal:
    """'New release' with promotions. Bug: assumes every product has a promotion rate."""
    discount = PROMOTION_RATES[product_id]  # KeyError for products without a promotion
    subtotal = price * quantity
    return (subtotal - subtotal * discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
