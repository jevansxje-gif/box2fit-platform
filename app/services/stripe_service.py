"""Stripe: Customer + SetupIntent on the GUARDIAN account (payment always
attaches to the adult). Card data touches Stripe Elements only. Test mode
until told otherwise. Attribution UTMs are mirrored into Stripe metadata."""
import logging

from flask import current_app

from ..extensions import db
from ..models import Lead, PaymentMethodStatus, StripeCustomer, User

log = logging.getLogger(__name__)


def stripe_client():
    import stripe

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


def is_configured() -> bool:
    return bool(current_app.config["STRIPE_SECRET_KEY"])


def ensure_customer_with_setup_intent(
    user: User, lead: Lead | None = None
) -> tuple[StripeCustomer, str | None]:
    """Create/fetch the guardian's StripeCustomer + a SetupIntent for card
    vaulting. Returns (customer, client_secret); client_secret is None when
    Stripe isn't configured (local dev)."""
    customer = (
        db.session.query(StripeCustomer).filter_by(user_id=user.id).one_or_none()
    )
    if customer is None:
        customer = StripeCustomer(user_id=user.id)
        db.session.add(customer)
        db.session.flush()

    if not is_configured():
        return customer, None

    stripe = stripe_client()
    metadata = {
        "user_id": str(user.id),
        "client_account_id": str(user.client_account_id),
    }
    if lead is not None:
        metadata.update(
            {
                "lead_id": str(lead.id),
                "segment": lead.segment or "",
                "utm_campaign": lead.utm_campaign or "",
                "utm_content": lead.utm_content or "",
            }
        )
    if not customer.stripe_customer_id:
        sc = stripe.Customer.create(
            name=user.name, email=user.email, phone=user.phone, metadata=metadata
        )
        customer.stripe_customer_id = sc.id

    si = stripe.SetupIntent.create(
        customer=customer.stripe_customer_id,
        usage="off_session",
        # Card only: suppresses the Stripe Link signup upsell inside the
        # Payment Element — guardians already gave us their details.
        payment_method_types=["card"],
        metadata=metadata,
    )
    customer.stripe_setup_intent_id = si.id
    return customer, si.client_secret


def confirm_setup_intent_vaulted(customer: StripeCustomer) -> bool:
    """Server-side verification on Elements return. Webhooks (Pass 2) are the
    source of truth; this covers the redirect path."""
    if not is_configured() or not customer.stripe_setup_intent_id:
        return False
    stripe = stripe_client()
    si = stripe.SetupIntent.retrieve(customer.stripe_setup_intent_id)
    if si.status == "succeeded":
        customer.payment_method_status = PaymentMethodStatus.vaulted.value
        customer.stripe_payment_method_id = si.payment_method
        return True
    if si.status == "canceled":
        customer.payment_method_status = PaymentMethodStatus.failed.value
    return False
