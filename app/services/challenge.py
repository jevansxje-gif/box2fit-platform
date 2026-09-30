"""The 5-Week Guided Boxing Challenge (adult cohort offer).

One up-front payment for five weeks (gloves, wraps and one personal session
included), then the regular membership continues every 4 weeks unless the
member cancels before the renewal date. Sold through one Stripe Checkout in
subscription mode: the challenge fee is a one-time line charged today and the
membership price sits behind a trial that ends on the renewal date.

Everything about the current cohort lives in CHALLENGE so the next one is a
config change, not a rebuild.
"""
import json
import logging
from datetime import date, datetime, time

from flask import render_template

from ..extensions import db
from ..models import (
    AttendeeProfile,
    PaymentMethodStatus,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
)
from . import stripe_service
from .tzutil import local_to_utc
from .urls import absolute_url

log = logging.getLogger(__name__)

CHALLENGE = {
    "key": "guided-2026-10-26",
    "name": "5-Week Beast Camp Challenge",
    "start": date(2026, 10, 26),      # first class (Monday)
    "end": date(2026, 11, 27),        # last class (Friday)
    "renew": date(2026, 11, 30),      # membership continues from here
    "price_cents": 24900,             # pre-tax, charged today
    "spots": 12,
    "class_time": "6:00 am",
    "class_days": "Monday to Friday",
    "reminder_hours": 72,             # notice before the renewal charge
}
LIVE = (
    SubscriptionStatus.pending.value,
    SubscriptionStatus.active.value,
    SubscriptionStatus.past_due.value,
)


def renew_at_utc() -> datetime:
    return local_to_utc(CHALLENGE["renew"], time(10, 0))


def spots_taken(client_account_id: int) -> int:
    return (
        db.session.query(Subscription)
        .filter(
            Subscription.client_account_id == client_account_id,
            Subscription.challenge_key == CHALLENGE["key"],
            Subscription.status.in_(LIVE),
        )
        .count()
    )


def spots_left(client_account_id: int) -> int:
    return max(0, CHALLENGE["spots"] - spots_taken(client_account_id))


def existing_sub(attendee: AttendeeProfile) -> Subscription | None:
    return (
        db.session.query(Subscription)
        .filter(
            Subscription.attendee_id == attendee.id,
            Subscription.status.in_(LIVE),
        )
        .first()
    )


def _local_sub(attendee: AttendeeProfile, stripe_subscription_id: str | None) -> Subscription:
    from .billing import default_plan

    sub = None
    if stripe_subscription_id:
        sub = (
            db.session.query(Subscription)
            .filter_by(stripe_subscription_id=stripe_subscription_id)
            .one_or_none()
        )
    if sub is None:
        sub = existing_sub(attendee)
    if sub is None:
        plan = default_plan(attendee.client_account_id)
        sub = Subscription(
            client_account_id=attendee.client_account_id,
            user_id=attendee.user_id,
            attendee_id=attendee.id,
            plan_id=plan.id,
            cohort_label="Beast Camp Challenge · 6 am",
            status=SubscriptionStatus.pending.value,
            mrr_cents=plan.price_cents,
            first_charge_at=renew_at_utc(),
            challenge_key=CHALLENGE["key"],
        )
        db.session.add(sub)
        db.session.flush()
    if stripe_subscription_id and not sub.stripe_subscription_id:
        sub.stripe_subscription_id = stripe_subscription_id
    return sub


def start_checkout(guardian: User, attendee: AttendeeProfile) -> str | None:
    """Stripe Checkout URL for the challenge, or None when Stripe isn't
    configured (dev/test: the caller finalizes locally)."""
    from .billing import default_plan, ensure_stripe_price
    from .tax import ensure_stripe_gst_rate

    if not stripe_service.is_configured():
        return None
    stripe = stripe_service.stripe_client()
    plan = default_plan(attendee.client_account_id)
    gst = ensure_stripe_gst_rate(stripe)
    customer = db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    who = (
        {"customer": customer.stripe_customer_id}
        if customer and customer.stripe_customer_id
        else {"customer_email": guardian.email}
    )
    meta = {
        "challenge_key": CHALLENGE["key"],
        "user_id": str(guardian.id),
        "attendee_id": str(attendee.id),
    }
    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[
            {"price": ensure_stripe_price(plan), "quantity": 1, "tax_rates": [gst]},
            {
                "quantity": 1,
                "tax_rates": [gst],
                "price_data": {
                    "currency": "cad",
                    "unit_amount": CHALLENGE["price_cents"],
                    "product_data": {
                        "name": "Box2Fit " + CHALLENGE["name"],
                        "description": "Five weeks of coached Beast Camp at 6 am, gloves, wraps and one personal training session.",
                    },
                },
            },
        ],
        subscription_data={
            "trial_end": int((renew_at_utc() - datetime(1970, 1, 1)).total_seconds()),
            "metadata": meta,
        },
        metadata=meta,
        success_url=absolute_url("funnel.challenge_done") + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=absolute_url("funnel.challenge_join"),
        stripe_version="2023-10-16",
        **who,
    )
    db.session.commit()
    return json.loads(str(session)).get("url")


def finalize(session: dict) -> Subscription | None:
    """Idempotent. Called from the return page and from the
    checkout.session.completed webhook: make the local subscription, mark the
    card as saved, and record the first payment through the normal invoice
    handler (which also sends the challenge welcome and the staff alert)."""
    from . import billing

    meta = session.get("metadata") or {}
    if meta.get("challenge_key") != CHALLENGE["key"]:
        return None
    if session.get("payment_status") not in (None, "paid", "no_payment_required"):
        return None
    attendee = db.session.get(AttendeeProfile, int(meta.get("attendee_id") or 0))
    if attendee is None:
        log.warning("challenge finalize: unknown attendee in session %s", session.get("id"))
        return None
    guardian = db.session.get(User, attendee.user_id)
    stripe_sub_id = session.get("subscription")
    if isinstance(stripe_sub_id, dict):
        stripe_sub_id = stripe_sub_id.get("id")
    sub = _local_sub(attendee, stripe_sub_id)

    customer = db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    if customer is None:
        customer = StripeCustomer(user_id=guardian.id)
        db.session.add(customer)
    cus_id = session.get("customer")
    if isinstance(cus_id, dict):
        cus_id = cus_id.get("id")
    if cus_id and not customer.stripe_customer_id:
        customer.stripe_customer_id = cus_id
    customer.payment_method_status = PaymentMethodStatus.vaulted.value

    if stripe_sub_id and stripe_service.is_configured():
        stripe = stripe_service.stripe_client()
        try:
            ss = json.loads(str(stripe.Subscription.retrieve(stripe_sub_id, stripe_version="2019-09-09")))
            if ss.get("default_payment_method"):
                customer.stripe_payment_method_id = ss["default_payment_method"]
            inv_id = ss.get("latest_invoice")
            if inv_id:
                inv = json.loads(str(stripe.Invoice.retrieve(inv_id, stripe_version="2019-09-09")))
                if inv.get("status") == "paid":
                    billing.handle_invoice_paid(inv)
        except Exception:  # noqa: BLE001 — the invoice webhook is the backstop
            log.exception("challenge finalize: could not read subscription %s", stripe_sub_id)
    return sub


def send_welcome(sub: Subscription) -> None:
    """Replaces the membership welcome for challenge sign-ups: what happens
    next for the member, and a staff alert to book the personal session."""
    from .booking_flow import admin_alert_recipients
    from .messaging import send_email, send_sms
    from .signed_links import SALT_SET_PASSWORD, make_token
    from .tax import fmt_cents, total_with_gst_cents

    attendee = db.session.get(AttendeeProfile, sub.attendee_id)
    guardian = attendee.guardian
    invite_url = absolute_url("portal.set_password", token=make_token(guardian.id, SALT_SET_PASSWORD))
    from ..models import utcnow

    guardian.invited_at = guardian.invited_at or utcnow()
    ctx = dict(
        guardian=guardian, attendee=attendee, sub=sub, c=CHALLENGE, invite_url=invite_url,
        renew_total=fmt_cents(total_with_gst_cents(sub.mrr_cents)),
        goal=(attendee.health_json or {}).get("challenge_goal"),
        notes=(attendee.health_json or {}).get("notes"),
    )
    send_email(
        guardian, guardian.email, "You're in: the 5-Week Beast Camp Challenge",
        render_template("emails/challenge_welcome.html", **ctx),
        "challenge_welcome", sub.client_account_id, attendee_id=attendee.id,
    )
    if guardian.phone:
        send_sms(
            guardian, guardian.phone,
            f"Box2Fit: you're in the 5-Week Challenge. First class {CHALLENGE['start'].strftime('%a %b %d')}, "
            f"{CHALLENGE['class_time']}. We'll be in touch to book your personal session.",
            "challenge_welcome", sub.client_account_id, attendee_id=attendee.id,
        )
    for to in admin_alert_recipients():
        send_email(
            None, to,
            f"Challenge sign-up: {guardian.name} ({spots_taken(sub.client_account_id)} of {CHALLENGE['spots']})",
            render_template("emails/challenge_admin.html", **ctx),
            "challenge_admin", sub.client_account_id,
        )


def due_renewal_reminders() -> list[Subscription]:
    """Challenge members whose membership renews within the notice window and
    who have not been reminded or already cancelled."""
    from datetime import timedelta

    from ..models import utcnow

    now = utcnow()
    return (
        db.session.query(Subscription)
        .filter(
            Subscription.challenge_key.isnot(None),
            Subscription.status == SubscriptionStatus.active.value,
            Subscription.cancel_requested_at.is_(None),
            Subscription.pre_charge_reminder_sent_at.is_(None),
            Subscription.first_charge_at > now,
            Subscription.first_charge_at <= now + timedelta(hours=CHALLENGE["reminder_hours"]),
        )
        .all()
    )
