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


def _d(obj) -> dict:
    """StripeObject has no .get() in this library — work on plain dicts."""
    import json

    return json.loads(str(obj))


def card_summary(user: User) -> dict | None:
    """What the front desk needs to answer "did my card go through?":
    every card Stripe holds for the guardian, which one it will actually
    CHARGE (subscription default, else customer default), the next charge
    date, and any failed payment awaiting retry. Live from Stripe, so it
    is the truth rather than our cached status. None when there's nothing
    to show (no Stripe customer yet); never raises — an outage becomes an
    'error' the page can display."""
    from datetime import datetime

    customer = (
        db.session.query(StripeCustomer).filter_by(user_id=user.id).one_or_none()
    )
    if not is_configured() or customer is None or not customer.stripe_customer_id:
        return None
    out = {"cards": [], "charging": None, "subs": [], "error": None}
    ts = lambda v: datetime.utcfromtimestamp(int(v)) if v else None  # noqa: E731
    try:
        stripe = stripe_client()
        pms = _d(stripe.PaymentMethod.list(
            customer=customer.stripe_customer_id, type="card"
        ))["data"]
        cus = _d(stripe.Customer.retrieve(customer.stripe_customer_id))
        cus_default = (cus.get("invoice_settings") or {}).get("default_payment_method")
        subs = (
            db.session.query(Subscription)
            .filter(
                Subscription.user_id == user.id,
                Subscription.stripe_subscription_id.isnot(None),
                Subscription.status.in_(LIVE_SUB_STATUSES),
            )
            .all()
        )
        sub_pm = None
        for sub in subs:
            # Pinned version: top-level period_end / invoice fields, as the
            # webhook handlers expect (the account default reshapes them).
            ss = _d(stripe.Subscription.retrieve(
                sub.stripe_subscription_id, stripe_version="2019-09-09"
            ))
            pm = ss.get("default_payment_method") or cus_default
            sub_pm = sub_pm or pm
            entry = {
                "status": ss.get("status"),
                "period_end": ts(ss.get("current_period_end")),
                "open_invoice": None,
            }
            open_invs = _d(stripe.Invoice.list(
                subscription=sub.stripe_subscription_id, status="open", limit=1,
                stripe_version="2019-09-09",
            ))["data"]
            if open_invs:
                inv = open_invs[0]
                entry["open_invoice"] = {
                    "amount_cents": inv.get("amount_due") or 0,
                    "attempts": inv.get("attempt_count") or 0,
                    "next_retry": ts(inv.get("next_payment_attempt")),
                }
            out["subs"].append(entry)
        charging_id = sub_pm or cus_default
        for pm in sorted(pms, key=lambda x: x.get("created") or 0, reverse=True):
            card = pm.get("card") or {}
            out["cards"].append({
                "brand": card.get("brand") or "card",
                "last4": card.get("last4") or "????",
                "exp": f"{card.get('exp_month')}/{str(card.get('exp_year'))[-2:]}",
                "added": ts(pm.get("created")),
                "is_charging": pm.get("id") == charging_id,
            })
        out["charging"] = next((c for c in out["cards"] if c["is_charging"]), None)
    except Exception:  # noqa: BLE001
        log.exception("card summary for user %s", user.id)
        out["error"] = "Couldn't reach Stripe just now"
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
