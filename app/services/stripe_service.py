"""Stripe: Customer + SetupIntent on the GUARDIAN account (payment always
attaches to the adult). Card data touches Stripe Elements only. Test mode
until told otherwise. Attribution UTMs are mirrored into Stripe metadata."""
import logging

from flask import current_app

from ..extensions import db
from ..models import (
    Lead,
    PaymentMethodStatus,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
)

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


LIVE_SUB_STATUSES = (
    SubscriptionStatus.pending.value,
    SubscriptionStatus.active.value,
    SubscriptionStatus.past_due.value,
)


def make_default_card(customer: StripeCustomer, pm_id: str | None) -> dict:
    """A newly saved card must become the card Stripe actually CHARGES.

    Subscriptions are created pinned to the card that existed at activation
    (default_payment_method), so vaulting a replacement card only on our side
    left Stripe charging — and retrying — the old one (found via a real
    member's card change, Sep 2026). Set the new card as the customer's
    invoice default and on every live subscription, and if a subscription is
    past due, pay its open invoice on the new card right now instead of
    waiting for Stripe's retry schedule. Best-effort: never breaks the
    'card saved' page — failures are logged and surface in the payment
    self-audit. Returns counts for the caller/script to report."""
    out = {"subs": 0, "retried": 0, "paid": 0, "errors": 0}
    if not is_configured() or not customer.stripe_customer_id or not pm_id:
        return out
    stripe = stripe_client()
    try:
        stripe.Customer.modify(
            customer.stripe_customer_id,
            invoice_settings={"default_payment_method": pm_id},
        )
    except Exception:  # noqa: BLE001
        log.exception("default card: customer %s", customer.stripe_customer_id)
        out["errors"] += 1
    subs = (
        db.session.query(Subscription)
        .filter(
            Subscription.user_id == customer.user_id,
            Subscription.stripe_subscription_id.isnot(None),
            Subscription.status.in_(LIVE_SUB_STATUSES),
        )
        .all()
    )
    for sub in subs:
        try:
            stripe.Subscription.modify(
                sub.stripe_subscription_id, default_payment_method=pm_id
            )
            out["subs"] += 1
        except Exception:  # noqa: BLE001
            log.exception("default card: sub %s", sub.stripe_subscription_id)
            out["errors"] += 1
            continue
        if sub.status != SubscriptionStatus.past_due.value:
            continue
        # Retry the failed charge on the new card immediately; the
        # invoice.paid webhook then records it and re-activates the member.
        try:
            open_invoices = stripe.Invoice.list(
                subscription=sub.stripe_subscription_id, status="open", limit=5
            ).data
        except Exception:  # noqa: BLE001
            log.exception(
                "default card: list invoices %s", sub.stripe_subscription_id
            )
            out["errors"] += 1
            continue
        for inv in open_invoices:
            out["retried"] += 1
            try:
                paid = stripe.Invoice.pay(inv.id, payment_method=pm_id)
                status = (
                    paid.get("status") if isinstance(paid, dict)
                    else getattr(paid, "status", None)
                )
                if status == "paid":
                    out["paid"] += 1
            except Exception:  # noqa: BLE001
                # Declined again -> stays past due; dunning already covers it.
                log.warning(
                    "default card: retry of %s failed", inv.id, exc_info=True
                )
    return out


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
        make_default_card(customer, si.payment_method)
        return True
    if si.status == "canceled":
        customer.payment_method_status = PaymentMethodStatus.failed.value
    return False
