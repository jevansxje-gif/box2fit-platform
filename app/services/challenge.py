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
    "name": "5-Week Guided Boxing Challenge",
    "start": date(2026, 10, 26),      # first class (Monday)
    "end": date(2026, 11, 27),        # last class (Friday)
    "renew": date(2026, 11, 30),      # membership continues from here
    "price_cents": 24900,             # pre-tax, charged today
    "spots": 12,
    "class_time": "6:00 am",
    "class_days": "Monday to Friday",
    "reminder_hours": 72,             # notice before the renewal charge
}
# The She Hits intro is the same mechanism on a rolling start: two weeks paid
# today, membership from day 15 unless cancelled. No cohort, no spot cap.
SHEHITS_INTRO = {
    "key": "shehits-intro",
    "name": "She Hits: two-week intro",
    "price_cents": 9450,
    "days": 14,
    "starts": date(2026, 10, 26),  # the class launches with the campaign; two weeks count from here
    "pass_segment": "shehits",
    "cohort_label": "She Hits · 9 am",
    "class_time": "9:00 am",
    "reminder_hours": 72,
    "description": "Two weeks of She Hits, any sessions, women only, coached.",
}
OFFERS = {CHALLENGE["key"]: CHALLENGE, SHEHITS_INTRO["key"]: SHEHITS_INTRO}


def _start_label(offer: dict) -> str:
    from .tzutil import today_local

    d = offer_start(offer)
    return "today" if d <= today_local() else d.strftime("%A, %B %d")


def offer_start(offer: dict) -> date:
    """First day of a rolling intro: today, or the program's launch date if later."""
    from .tzutil import today_local

    t = today_local()
    s = offer.get("starts")
    return s if s and s > t else t


def offer_renew_at(offer: dict) -> datetime:
    """When the membership starts charging: the cohort's fixed date, or day
    15 of a rolling intro (10 am local, so the reminder lands in daytime)."""
    from datetime import timedelta

    if offer.get("renew"):
        return local_to_utc(offer["renew"], time(10, 0))
    return local_to_utc(offer_start(offer) + timedelta(days=offer["days"]), time(10, 0))


LIVE = (
    SubscriptionStatus.pending.value,
    SubscriptionStatus.active.value,
    SubscriptionStatus.past_due.value,
)


def renew_at_utc() -> datetime:
    return offer_renew_at(CHALLENGE)


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


def _local_sub(attendee: AttendeeProfile, stripe_subscription_id: str | None, offer: dict | None = None,
               renew_at: datetime | None = None) -> Subscription:
    from .billing import default_plan

    offer = offer or CHALLENGE

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
            cohort_label=offer.get("cohort_label", "Guided Boxing Challenge · 6 am"),
            status=SubscriptionStatus.pending.value,
            mrr_cents=plan.price_cents,
            first_charge_at=renew_at or offer_renew_at(offer),
            challenge_key=offer["key"],
        )
        db.session.add(sub)
        db.session.flush()
    if offer.get("pass_segment") and sub.first_charge_at:
        # Book that program's classes through the trial flow until renewal.
        from .tzutil import utc_to_local

        attendee.pass_until = utc_to_local(sub.first_charge_at).date()
        attendee.pass_segment = offer["pass_segment"]
    if stripe_subscription_id and not sub.stripe_subscription_id:
        sub.stripe_subscription_id = stripe_subscription_id
    return sub


def start_checkout(guardian: User, attendee: AttendeeProfile, offer: dict | None = None,
                   success_endpoint: str = "funnel.challenge_done", cancel_endpoint: str = "funnel.challenge_join") -> str | None:
    """Stripe Checkout URL for a paid intro (challenge or She Hits), or None
    when Stripe isn't configured (dev/test: the caller finalizes locally)."""
    from .billing import default_plan, ensure_stripe_price
    from .tax import ensure_stripe_gst_rate

    offer = offer or CHALLENGE
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
        "challenge_key": offer["key"],
        "user_id": str(guardian.id),
        "attendee_id": str(attendee.id),
    }
    renew_at = offer_renew_at(offer)
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
                    "unit_amount": offer["price_cents"],
                    "product_data": {
                        "name": "Box2Fit " + offer["name"],
                        "description": offer.get("description", "Five weeks of coached Guided Boxing at 6 am, gloves, wraps and one personal training session."),
                    },
                },
            },
        ],
        subscription_data={
            "trial_end": int((renew_at - datetime(1970, 1, 1)).total_seconds()),
            "metadata": meta,
        },
        metadata=meta,
        success_url=absolute_url(success_endpoint) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=absolute_url(cancel_endpoint),
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
    offer = OFFERS.get(meta.get("challenge_key") or "")
    if offer is None:
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
    renew_at = None
    if stripe_sub_id and stripe_service.is_configured():
        try:
            ss0 = json.loads(str(stripe_service.stripe_client().Subscription.retrieve(stripe_sub_id, stripe_version="2019-09-09")))
            if ss0.get("trial_end"):
                renew_at = datetime.utcfromtimestamp(int(ss0["trial_end"]))
        except Exception:  # noqa: BLE001
            log.exception("finalize: could not read trial_end for %s", stripe_sub_id)
    sub = _local_sub(attendee, stripe_sub_id, offer, renew_at)

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
    offer = OFFERS.get(sub.challenge_key or "", CHALLENGE)
    invite_url = absolute_url("portal.set_password", token=make_token(guardian.id, SALT_SET_PASSWORD))
    from ..models import utcnow

    guardian.invited_at = guardian.invited_at or utcnow()
    from . import guided

    g = (attendee.health_json or {}).get("guided")
    ctx = dict(
        guardian=guardian, attendee=attendee, sub=sub, c=CHALLENGE, o=offer, invite_url=invite_url,
        guided_url=guided.url_for_attendee(attendee.id),
        renew_total=fmt_cents(total_with_gst_cents(sub.mrr_cents)),
        renew_when=sub.first_charge_at,
        start_label=_start_label(offer),
        goal=(attendee.health_json or {}).get("challenge_goal") or (g or {}).get("success"),
        notes=(attendee.health_json or {}).get("notes") or (g or {}).get("notes"),
        guided_summary=guided.summary(g),
    )
    if offer is SHEHITS_INTRO:
        from .tzutil import fmt_local

        send_email(
            guardian, guardian.email, "You're in: two weeks of She Hits",
            render_template("emails/shehits_welcome.html", **ctx),
            "shehits_welcome", sub.client_account_id, attendee_id=attendee.id,
        )
        if guardian.phone:
            send_sms(
                guardian, guardian.phone,
                f"Box2Fit: you're in for two weeks of She Hits, weekdays 9 am. Book your first session: "
                f"{absolute_url('funnel.step_class', segment='shehits')}",
                "shehits_welcome", sub.client_account_id, attendee_id=attendee.id,
            )
        for to in admin_alert_recipients():
            send_email(
                None, to, f"She Hits two-week sign-up: {guardian.name}",
                render_template("emails/shehits_admin.html", **ctx),
                "shehits_admin", sub.client_account_id,
            )
        return
    send_email(
        guardian, guardian.email, "You're in: the 5-Week Guided Boxing Challenge",
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
