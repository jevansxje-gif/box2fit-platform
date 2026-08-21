"""GST (5%, Canada-wide) on all purchases — client requirement 2026-08-21.

BC gym memberships and class packs attract GST only (no BC PST on fitness
services). Prices everywhere in the platform stay PRE-TAX; tax is added on
top at charge time via a Stripe TaxRate, so Stripe invoices carry a proper
GST line item. The agency commission is computed on pre-tax revenue only —
collected tax is the government's money, never commissionable.

Compliance display: customer-facing prices show "X + 5% GST (total)" and
receipts must carry the gym's GST registration number — set GST_NUMBER in
the environment once the client provides it (and add it as a Tax ID in the
Stripe dashboard so Stripe-generated invoices/receipts include it too).
"""
from flask import current_app

from ..models import SiteSetting

GST_RATE = 0.05
GST_PERCENT = 5.0  # what Stripe's TaxRate wants


def gst_cents(pre_tax_cents: int) -> int:
    return round(pre_tax_cents * GST_RATE)


def total_with_gst_cents(pre_tax_cents: int) -> int:
    return pre_tax_cents + gst_cents(pre_tax_cents)


def fmt_cents(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def price_with_gst_label(pre_tax_cents: int) -> str:
    """'$189 + 5% GST ($198.45)' — the customer-facing price string."""
    return (
        f"{fmt_cents(pre_tax_cents)} + 5% GST "
        f"({fmt_cents(total_with_gst_cents(pre_tax_cents))})"
    )


def gst_number() -> str:
    return current_app.config.get("GST_NUMBER", "") or ""


def ensure_stripe_gst_rate(stripe) -> str:
    """Find-or-create the 5% GST TaxRate in Stripe. Cached per mode
    (test/live) so a mode switch creates a fresh rate instead of pointing
    at the other mode's object."""
    mode = "live" if "live" in (stripe.api_key or "")[:8] else "test"
    key = f"stripe_gst_tax_rate_{mode}"
    rate_id = SiteSetting.get(key, "")
    if rate_id:
        return rate_id
    rate = stripe.TaxRate.create(
        display_name="GST",
        percentage=GST_PERCENT,
        inclusive=False,
        country="CA",
        description="Goods and Services Tax (Canada, 5%)",
    )
    SiteSetting.set(key, rate.id)
    return rate.id
