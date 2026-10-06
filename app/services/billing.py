"""Money lifecycle (Pass 2).

Activation creates the Stripe subscription on the guardian's vaulted card:
- interval = week × 4 (client-confirmed 4-week billing cycle, $189)
- trial_end = now + PRE_CHARGE_LEAD_HOURS (48h) so the first charge lands
  after the mandatory pre-charge reminder (card-network trial rule)
- off_session on the stored default payment method; SCA falls back to
  Stripe's hosted confirmation and surfaces via webhooks

Webhook handlers are plain functions here so the receiver route stays thin
and the E2E can drive them with synthetic events. Every payment writes
agency_share_cents at the client account's commission rate (25%).
"""
import logging
from datetime import datetime, timedelta

from flask import current_app, render_template

from ..extensions import db
from ..models import (
    AttendeeKind,
    AttendeeProfile,
    ClientAccount,
    Lead,
    LeadStatus,
    Payment,
    PaymentMethodStatus,
    Plan,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from . import stripe_service
from .messaging import send_email, send_sms
from .signed_links import (
    SALT_CANCEL_BEFORE_CHARGE,
    SALT_UPDATE_CARD,
    make_token,
)
from .tracking import enqueue_event
from .urls import absolute_url

log = logging.getLogger(__name__)

STRIPE_INTERVAL = {"4_weeks": {"interval": "week", "interval_count": 4},
                   "5_weeks": {"interval": "week", "interval_count": 5},
                   "month": {"interval": "month", "interval_count": 1}}
INTERVAL_LABEL = {"4_weeks": "every 4 weeks", "5_weeks": "every 5 weeks", "month": "every month"}


def interval_label(plan) -> str:
    return INTERVAL_LABEL.get(getattr(plan, "interval", None), "every 4 weeks")


class ActivationError(Exception):
    pass


def default_plan(client_account_id: int) -> Plan | None:
    """The regular membership. Offer-specific plans (the 5-week challenge
    block) are picked by name in challenge.offer_plan, never as a default."""
    return (
        db.session.query(Plan)
        .filter_by(client_account_id=client_account_id, active=True)
        .filter(Plan.interval != "5_weeks")
        .order_by(Plan.class_type_id.desc())
        .first()
    )


def ensure_stripe_price(plan: Plan) -> str:
    """Create the Stripe Price for a plan lazily; cache the id."""
    if plan.stripe_price_id:
        return plan.stripe_price_id
    stripe = stripe_service.stripe_client()
    recurring = STRIPE_INTERVAL.get(plan.interval, STRIPE_INTERVAL["4_weeks"])
    price = stripe.Price.create(
        unit_amount=plan.price_cents,
        currency=plan.currency.lower(),
        recurring=recurring,
        product_data={"name": f"Box2Fit {plan.name}"},
    )
    plan.stripe_price_id = price.id
    return price.id


# FAMILY PRICING (client deal, 2026-08-28): 1st member full price, then
# $139 / $119 / $100 per 4-week cycle for the 2nd / 3rd / 4th+ member.
# "Family" = subscriptions under the SAME guardian account, which by
# construction all bill the same vaulted card — no promo codes needed and
# nothing for the parent to redeem; the tier applies automatically at
# activation. Tier is fixed at activation time (no auto-reprice when a
# sibling cancels — staff can adjust manually; open business rule).
FAMILY_TIER_CENTS = (13900, 11900, 10000)  # 2nd, 3rd, 4th-and-beyond


def family_price_cents(existing_family_subs: int, plan: Plan) -> int:
    if existing_family_subs <= 0:
        return plan.price_cents
    return FAMILY_TIER_CENTS[min(existing_family_subs - 1, len(FAMILY_TIER_CENTS) - 1)]


def ensure_stripe_price_cents(plan: Plan, cents: int) -> str:
    """Find-or-create a Stripe Price for a family-tier amount. Cached per
    test/live mode in SiteSetting (same pattern as the GST tax rate)."""
    if cents == plan.price_cents:
        return ensure_stripe_price(plan)
    from ..models import SiteSetting

    stripe = stripe_service.stripe_client()
    mode = "live" if "live" in (stripe.api_key or "")[:8] else "test"
    key = f"stripe_price_{cents}_{mode}"
    price_id = SiteSetting.get(key, "")
    if price_id:
        return price_id
    recurring = STRIPE_INTERVAL.get(plan.interval, STRIPE_INTERVAL["4_weeks"])
    price = stripe.Price.create(
        unit_amount=cents,
        currency=plan.currency.lower(),
        recurring=recurring,
        product_data={"name": f"Box2Fit {plan.name} — family rate ${cents // 100}"},
    )
    SiteSetting.set(key, price.id)
    return price.id


def activate_subscription(
    attendee: AttendeeProfile,
    plan: Plan | None = None,
    cohort_label: str | None = None,
    actor: str = "member",
    first_charge_on=None,
) -> Subscription:
    """Create the subscription on the vaulted card. One subscription per
    enrolled attendee, billed to the guardian."""
    guardian = attendee.guardian
    plan = plan or default_plan(attendee.client_account_id)
    if plan is None:
        raise ActivationError("No active plan configured.")

    existing = (
        db.session.query(Subscription)
        .filter(
            Subscription.attendee_id == attendee.id,
            Subscription.status.in_(
                [
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]
            ),
        )
        .first()
    )
    if existing:
        raise ActivationError("This attendee already has a membership.")

    customer = (
        db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    )
    if customer is None or customer.payment_method_status != PaymentMethodStatus.vaulted.value:
        raise ActivationError(
            "No card on file yet — add a card before activating."
        )

    lead_hours = current_app.config["PRE_CHARGE_LEAD_HOURS"]
    first_charge_at = utcnow() + timedelta(hours=lead_hours)
    if first_charge_on is not None:
        # Staff-agreed payday (e.g. "take it on the 21st"): charge at
        # 10 AM local that day — but never shorter than the standard
        # pre-charge notice window.
        from datetime import time as dtime

        from .tzutil import local_to_utc

        candidate = local_to_utc(first_charge_on, dtime(10, 0))
        if candidate > first_charge_at:
            first_charge_at = candidate

    # Family pricing: count the guardian's OTHER live subscriptions —
    # same account means same card, so this is the family by construction.
    siblings = (
        db.session.query(Subscription)
        .filter(
            Subscription.user_id == guardian.id,
            Subscription.attendee_id != attendee.id,
            Subscription.status.in_(
                [
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]
            ),
        )
        .count()
    )
    tier_cents = family_price_cents(siblings, plan)

    sub = Subscription(
        client_account_id=attendee.client_account_id,
        user_id=guardian.id,
        attendee_id=attendee.id,
        plan_id=plan.id,
        cohort_label=cohort_label,
        status=SubscriptionStatus.pending.value,
        mrr_cents=tier_cents,
        first_charge_at=first_charge_at,
    )
    db.session.add(sub)
    db.session.flush()

    if stripe_service.is_configured():
        stripe = stripe_service.stripe_client()
        price_id = ensure_stripe_price_cents(plan, tier_cents)
        from .tax import ensure_stripe_gst_rate

        ssub = stripe.Subscription.create(
            customer=customer.stripe_customer_id,
            items=[{"price": price_id}],
            default_tax_rates=[ensure_stripe_gst_rate(stripe)],  # +5% GST
            default_payment_method=customer.stripe_payment_method_id,
            # first_charge_at is naive UTC; convert to epoch for Stripe
            trial_end=int((first_charge_at - datetime(1970, 1, 1)).total_seconds()),
            payment_behavior="allow_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            metadata={
                "attendee_id": str(attendee.id),
                "user_id": str(guardian.id),
                "cohort": cohort_label or "",
                "activated_by": actor,
            },
        )
        sub.stripe_subscription_id = ssub.id

    _send_pre_charge_reminder(sub)
    enqueue_event(
        "MembershipActivationStarted",
        attendee.client_account_id,
        _lead_for(guardian),
        subscription_id=sub.id,
    )
    return sub


def _lead_for(guardian: User) -> Lead | None:
    return (
        db.session.query(Lead)
        .filter_by(user_id=guardian.id)
        .order_by(Lead.id.desc())
        .first()
    )


def _send_pre_charge_reminder(sub: Subscription) -> None:
    """Sent at activation = PRE_CHARGE_LEAD_HOURS before the first charge.
    Card-network trial rules require a reminder before charging."""
    attendee = db.session.get(AttendeeProfile, sub.attendee_id)
    guardian = attendee.guardian
    plan = db.session.get(Plan, sub.plan_id)
    cancel_url = absolute_url(
        "funnel.cancel_membership",
        token=make_token(sub.id, SALT_CANCEL_BEFORE_CHARGE),
    )
    from .tax import price_with_gst_label

    price = price_with_gst_label(sub.mrr_cents)
    if sub.mrr_cents < plan.price_cents:
        # tuck inside the parens — the template continues "...billed every
        # 4 weeks" right after this string
        price = price[:-1] + ", family rate)"
    lead_hours = current_app.config["PRE_CHARGE_LEAD_HOURS"]
    # A staff-agreed payday pushes the charge past the standard window —
    # then the reminder must state the actual date, not "48 hours".
    from .tzutil import fmt_local

    if sub.first_charge_at <= utcnow() + timedelta(hours=lead_hours + 1):
        when = f"in {lead_hours} hours"
    else:
        when = "on " + fmt_local(sub.first_charge_at, "%A, %B %d")
    is_child = attendee.kind == AttendeeKind.child.value
    html = render_template(
        "emails/pre_charge_reminder.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        price=price,
        when=when,
        cohort=sub.cohort_label,
        cancel_url=cancel_url,
        every=interval_label(plan),
        is_block=plan.interval == "5_weeks",
    )
    who = f"{attendee.first_name}'s" if is_child else "Your"
    send_email(
        guardian, guardian.email,
        f"{who} next 5-week block starts {when}" if plan.interval == "5_weeks" else f"{who} Box2Fit membership starts {when}",
        html, "pre_charge_reminder", sub.client_account_id,
        attendee_id=attendee.id,
    )
    send_sms(
        guardian, guardian.phone,
        f"Box2Fit: {who.lower()} membership ({price} every 4 weeks) starts "
        f"{when}. Cancel before the charge in one click: {cancel_url}",
        "pre_charge_reminder", sub.client_account_id, attendee_id=attendee.id,
    )
    sub.pre_charge_reminder_sent_at = utcnow()


NOTICE_DAYS = 30  # mandatory cancellation notice per the membership terms


def cancel_subscription(
    sub: Subscription, reason: str, note: str | None = None, immediate: bool = False
) -> None:
    """Cancel per the client's membership terms:
    - BEFORE the first charge (free-trial window): immediate, no obligation.
    - AFTER activation: a 30-day notice is recorded; dues continue through
      the notice period and the subscription ends at the effective date
      (Stripe cancel_at; the subscription.deleted webhook finalizes it).
    - immediate=True (staff goodwill, e.g. alongside a refund): ends now,
      no notice period, no further charges. The refund itself is issued in
      Stripe; the charge.refunded webhook records it.
    """
    if sub.status == SubscriptionStatus.cancelled.value or sub.cancel_requested_at:
        return
    sub.cancel_reason = reason
    sub.cancel_reason_note = note

    # Paid challenge, before the membership renews: the weeks already paid
    # for stand, the renewal simply never happens. No notice period.
    in_challenge = bool(
        sub.challenge_key and sub.first_charge_at and utcnow() < sub.first_charge_at
    )
    if in_challenge and not immediate:
        sub.cancel_requested_at = utcnow()
        sub.cancel_effective_at = sub.first_charge_at
        if sub.stripe_subscription_id and stripe_service.is_configured():
            stripe = stripe_service.stripe_client()
            try:
                stripe.Subscription.modify(
                    sub.stripe_subscription_id, cancel_at_period_end=True
                )
            except Exception:
                log.exception("stripe cancel-at-renewal failed (id=%s)", sub.id)
        return

    never_charged = sub.activated_at is None
    if never_charged or immediate:
        if sub.stripe_subscription_id and stripe_service.is_configured():
            stripe = stripe_service.stripe_client()
            try:
                stripe.Subscription.cancel(sub.stripe_subscription_id)
            except Exception:
                log.exception("stripe subscription cancel failed (id=%s)", sub.id)
        sub.status = SubscriptionStatus.cancelled.value
        sub.cancelled_at = utcnow()
        return

    sub.cancel_requested_at = utcnow()
    sub.cancel_effective_at = utcnow() + timedelta(days=NOTICE_DAYS)
    if sub.stripe_subscription_id and stripe_service.is_configured():
        stripe = stripe_service.stripe_client()
        try:
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                cancel_at=int(
                    (sub.cancel_effective_at - datetime(1970, 1, 1)).total_seconds()
                ),
            )
        except Exception:
            log.exception("stripe cancel_at scheduling failed (id=%s)", sub.id)


# ---------------------------------------------------------- one-off charges ---
# Staff type an amount + description (e.g. "10-class punch card"); the customer
# gets a signed link, pays on Stripe Checkout, and staff are emailed when the
# money lands. Usage of what was bought is the gym's to track.

def create_one_off_charge(client_account_id, guardian, description, amount_cents, created_by=None,
                          attendee=None, pass_days=None, pass_segment=None):
    from ..models import OneOffCharge
    from .tax import gst_cents

    tax = gst_cents(amount_cents)
    charge = OneOffCharge(
        attendee_id=attendee.id if attendee is not None else None,
        pass_days=pass_days,
        pass_segment=pass_segment,
        client_account_id=client_account_id,
        user_id=guardian.id,
        description=(description or "One-off payment").strip()[:120],
        amount_cents=amount_cents,
        tax_cents=tax,
        total_cents=amount_cents + tax,
        created_by=created_by,
    )
    db.session.add(charge)
    db.session.flush()
    return charge


def one_off_pay_url(charge) -> str:
    from .signed_links import SALT_ONE_OFF

    return absolute_url("funnel.one_off_pay", token=make_token(charge.id, SALT_ONE_OFF))


def send_one_off_link(charge, guardian) -> str:
    from .tax import fmt_cents

    link = one_off_pay_url(charge)
    html = render_template(
        "emails/one_off_link.html", guardian=guardian, charge=charge, link=link, fmt=fmt_cents
    )
    send_email(
        guardian, guardian.email,
        f"Your Box2Fit payment link: {charge.description}",
        html, "one_off_link", charge.client_account_id,
    )
    if guardian.phone:
        send_sms(
            guardian, guardian.phone,
            f"Box2Fit: pay for {charge.description} ({fmt_cents(charge.total_cents)} incl. GST) here: {link}",
            "one_off_link", charge.client_account_id,
        )
    return link


def start_one_off_checkout(charge, guardian, success_url=None, cancel_url=None) -> str | None:
    """Create the Stripe Checkout Session for this charge and return its URL
    (None when Stripe isn't configured). Uses a current API version for this
    call only, because the account default (2019-09-09) predates Checkout's
    hosted URL; the webhook payload shape is unaffected."""
    import json

    from .signed_links import SALT_ONE_OFF
    from .tax import fmt_cents

    if not stripe_service.is_configured():
        return None
    stripe = stripe_service.stripe_client()
    token = make_token(charge.id, SALT_ONE_OFF)
    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=guardian.email,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": charge.currency.lower(),
                "unit_amount": charge.total_cents,
                "product_data": {
                    "name": f"Box2Fit: {charge.description}",
                    "description": f"Includes 5% GST ({fmt_cents(charge.tax_cents)})",
                },
            },
        }],
        success_url=(success_url or absolute_url("funnel.one_off_done", token=token)) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=cancel_url or absolute_url("funnel.one_off_pay", token=token),
        metadata={"one_off_charge_id": str(charge.id)},
        payment_intent_data={
            "description": f"Box2Fit {charge.description}",
            "metadata": {"one_off_charge_id": str(charge.id)},
        },
        stripe_version="2023-10-16",
    )
    sd = json.loads(str(session))
    charge.stripe_checkout_session_id = sd.get("id")
    return sd.get("url")


def record_one_off_paid(charge, payment_intent_id=None) -> Payment:
    """Idempotent: the ledger row, the staff alert and the customer receipt."""
    from .booking_flow import admin_alert_recipients
    from .tax import fmt_cents

    if charge.status == "paid" and charge.payment_id:
        return db.session.get(Payment, charge.payment_id)
    client = db.session.get(ClientAccount, charge.client_account_id)
    guardian = db.session.get(User, charge.user_id)
    if isinstance(payment_intent_id, dict):
        payment_intent_id = payment_intent_id.get("id")
    pay = Payment(
        client_account_id=charge.client_account_id,
        user_id=charge.user_id,
        subscription_id=None,
        stripe_charge_id=payment_intent_id or charge.stripe_payment_intent_id,
        amount_cents=charge.total_cents,
        tax_cents=charge.tax_cents,
        currency=charge.currency,
        status="paid",
        agency_share_cents=round(charge.amount_cents * client.commission_rate),
        paid_at=utcnow(),
        note=f"one-off: {charge.description}",
    )
    db.session.add(pay)
    db.session.flush()
    charge.status = "paid"
    charge.paid_at = utcnow()
    charge.payment_id = pay.id
    if charge.attendee_id and charge.pass_days:
        from .tzutil import today_local

        att = db.session.get(AttendeeProfile, charge.attendee_id)
        if att is not None:
            att.pass_until = today_local() + timedelta(days=charge.pass_days - 1)
            att.pass_segment = charge.pass_segment
    charge.stripe_payment_intent_id = payment_intent_id or charge.stripe_payment_intent_id

    ctx = dict(guardian=guardian, charge=charge, fmt=fmt_cents)
    send_email(
        guardian, guardian.email,
        f"Payment received: {charge.description}",
        render_template("emails/one_off_receipt.html", **ctx),
        "one_off_receipt", charge.client_account_id,
    )
    for to in admin_alert_recipients():
        send_email(
            None, to,
            f"Paid: {charge.description}, {fmt_cents(charge.total_cents)} from {guardian.name}",
            render_template("emails/one_off_paid_admin.html", **ctx),
            "one_off_paid_admin", charge.client_account_id,
        )
    return pay


def handle_checkout_completed(obj: dict) -> None:
    """Stripe `checkout.session.completed` for a one-off charge."""
    from ..models import OneOffCharge

    meta = obj.get("metadata") or {}
    if meta.get("challenge_key"):
        from . import challenge

        challenge.finalize(obj)
        return
    charge = None
    if meta.get("one_off_charge_id"):
        charge = db.session.get(OneOffCharge, int(meta["one_off_charge_id"]))
    if charge is None and obj.get("id"):
        charge = (
            db.session.query(OneOffCharge)
            .filter_by(stripe_checkout_session_id=obj.get("id"))
            .one_or_none()
        )
    if charge is None:
        log.warning("checkout.session.completed for unknown charge: %s", obj.get("id"))
        return
    if obj.get("payment_status") not in (None, "paid"):
        return  # not paid yet (delayed methods); an async_payment_succeeded would follow
    record_one_off_paid(charge, obj.get("payment_intent"))


def confirm_one_off_return(charge, session_id) -> bool:
    """Customer came back from Checkout: verify with Stripe and record, so we
    never depend on the webhook alone (lesson of September)."""
    import json

    if charge.status == "paid":
        return True
    if not session_id or not stripe_service.is_configured():
        return False
    stripe = stripe_service.stripe_client()
    try:
        sd = json.loads(str(stripe.checkout.Session.retrieve(session_id, stripe_version="2023-10-16")))
    except Exception:  # noqa: BLE001
        log.exception("one-off: could not retrieve checkout session %s", session_id)
        return False
    if sd.get("payment_status") == "paid" and (sd.get("metadata") or {}).get("one_off_charge_id") == str(charge.id):
        record_one_off_paid(charge, sd.get("payment_intent"))
        return True
    return False


def guardian_is_past_due(guardian_user_id: int) -> bool:
    """Past-due blocks booking (resolved policy). Checked at booking time."""
    return (
        db.session.query(Subscription)
        .filter_by(user_id=guardian_user_id, status=SubscriptionStatus.past_due.value)
        .count()
        > 0
    )


def card_update_url(guardian: User) -> str:
    return absolute_url(
        "portal.update_card", token=make_token(guardian.id, SALT_UPDATE_CARD)
    )


# ------------------------------------------------------------ webhooks ------
def handle_setup_intent_succeeded(obj: dict) -> None:
    si_id = obj.get("id")
    customer = (
        db.session.query(StripeCustomer)
        .filter_by(stripe_setup_intent_id=si_id)
        .one_or_none()
    )
    if customer is None:
        return
    already_vaulted = (
        customer.payment_method_status == PaymentMethodStatus.vaulted.value
    )
    customer.payment_method_status = PaymentMethodStatus.vaulted.value
    customer.stripe_payment_method_id = obj.get("payment_method")
    stripe_service.make_default_card(customer, customer.stripe_payment_method_id)
    if not already_vaulted:
        guardian = db.session.get(User, customer.user_id)
        enqueue_event(
            "AddPaymentInfo", guardian.client_account_id, _lead_for(guardian)
        )


def handle_invoice_paid(obj: dict) -> Payment | None:
    invoice_id = obj.get("id")
    sub_id = obj.get("subscription")
    amount = obj.get("amount_paid", 0)
    tax = obj.get("tax") or 0  # GST portion of amount — never commissionable
    if not sub_id:
        return None
    sub = (
        db.session.query(Subscription)
        .filter_by(stripe_subscription_id=sub_id)
        .one_or_none()
    )
    if sub is None:
        log.warning("invoice.paid for unknown subscription %s", sub_id)
        return None
    client = db.session.get(ClientAccount, sub.client_account_id)
    existing = (
        db.session.query(Payment).filter_by(stripe_invoice_id=invoice_id).one_or_none()
    )
    if existing and existing.status == "paid":
        return existing  # idempotent on redelivery
    payment = existing or Payment(
        client_account_id=sub.client_account_id,
        user_id=sub.user_id,
        subscription_id=sub.id,
        stripe_invoice_id=invoice_id,
    )
    # A retry that finally succeeds promotes the earlier 'failed' row to paid.
    payment.stripe_charge_id = obj.get("charge")
    payment.amount_cents = amount
    payment.tax_cents = tax
    payment.currency = (obj.get("currency") or "cad").upper()
    payment.status = "paid"
    payment.agency_share_cents = round(max(0, amount - tax) * client.commission_rate)
    payment.paid_at = utcnow()
    payment.note = None
    if existing is None:
        db.session.add(payment)

    first_payment = sub.activated_at is None
    was_past_due = sub.status == SubscriptionStatus.past_due.value
    sub.status = SubscriptionStatus.active.value
    if first_payment:
        sub.activated_at = utcnow()
    period_end = obj.get("period_end") or (obj.get("lines", {}) or {}).get(
        "data", [{}]
    )[0].get("period", {}).get("end")
    if period_end:
        sub.current_period_end = datetime.utcfromtimestamp(int(period_end))

    guardian = db.session.get(User, sub.user_id)
    lead = _lead_for(guardian)
    if lead:
        lead.status = LeadStatus.activated.value

    if first_payment:
        # The event campaigns optimize toward (value = plan price, CAD)
        plan = db.session.get(Plan, sub.plan_id)
        enqueue_event(
            "SubscriptionActivated",
            sub.client_account_id,
            lead,
            value=plan.price_cents / 100,
            currency="CAD",
            subscription_id=sub.id,
        )
        if sub.challenge_key:
            from . import challenge

            challenge.send_welcome(sub)
        else:
            _send_welcome(sub)
    elif was_past_due:
        _send_recovered(sub)
    return payment


def handle_invoice_payment_paid(obj: dict) -> Payment | None:
    """Adapter for Stripe's newer `invoice_payment.paid` event, which this
    account's webhook endpoint sends for successful charges instead of the
    classic `invoice.paid`. Its object is an InvoicePayment (only an `invoice`
    reference — no subscription/tax/lines), so we fetch the full invoice on the
    pinned API version and hand it to the invoice.paid handler unchanged."""
    import json

    inv_ref = obj.get("invoice")
    if isinstance(inv_ref, dict):  # expanded — already the invoice
        return handle_invoice_paid(inv_ref)
    if not inv_ref or not stripe_service.is_configured():
        return None
    st = stripe_service.stripe_client()
    # The handler reads top-level subscription/tax/period_end/lines — shapes
    # the account-default (newer) API version drops, so pin the old one.
    st.api_version = "2019-09-09"
    invoice = json.loads(str(st.Invoice.retrieve(inv_ref)))
    return handle_invoice_paid(invoice)


def handle_invoice_payment_failed(obj: dict) -> None:
    sub_id = obj.get("subscription")
    if not sub_id:
        return
    sub = (
        db.session.query(Subscription)
        .filter_by(stripe_subscription_id=sub_id)
        .one_or_none()
    )
    if sub is None:
        return
    sub.status = SubscriptionStatus.past_due.value
    # Record the failed attempt so admins can reconcile it (upsert on the
    # invoice — a later retry that succeeds promotes this same row to paid).
    invoice_id = obj.get("id")
    amount = obj.get("amount_due") or obj.get("total") or 0
    if invoice_id:
        pay = (
            db.session.query(Payment)
            .filter_by(stripe_invoice_id=invoice_id)
            .one_or_none()
        )
        if pay is None:
            pay = Payment(
                client_account_id=sub.client_account_id,
                user_id=sub.user_id,
                subscription_id=sub.id,
                stripe_invoice_id=invoice_id,
            )
            db.session.add(pay)
        if pay.status != "paid":
            pay.amount_cents = amount
            pay.tax_cents = obj.get("tax") or 0
            pay.currency = (obj.get("currency") or "cad").upper()
            pay.status = "failed"
            pay.agency_share_cents = 0
            pay.stripe_charge_id = obj.get("charge")
            pay.note = "payment failed — card declined or expired"
    guardian = db.session.get(User, sub.user_id)
    attendee = db.session.get(AttendeeProfile, sub.attendee_id)
    update_url = card_update_url(guardian)
    html = render_template(
        "emails/dunning.html",
        guardian=guardian,
        attendee=attendee,
        update_url=update_url,
    )
    send_email(
        guardian, guardian.email,
        "Quick card update needed — Box2Fit",
        html, "dunning", sub.client_account_id, attendee_id=attendee.id,
    )
    send_sms(
        guardian, guardian.phone,
        f"Box2Fit: a payment didn't go through. Quick card update (takes a "
        f"minute) and you're all set: {update_url}",
        "dunning", sub.client_account_id, attendee_id=attendee.id,
    )
    enqueue_event("PaymentFailed", sub.client_account_id, _lead_for(guardian))


def handle_subscription_deleted(obj: dict) -> None:
    sub = (
        db.session.query(Subscription)
        .filter_by(stripe_subscription_id=obj.get("id"))
        .one_or_none()
    )
    if sub is None or sub.status == SubscriptionStatus.cancelled.value:
        return
    sub.status = SubscriptionStatus.cancelled.value
    sub.cancelled_at = utcnow()
    if not sub.cancel_reason:
        sub.cancel_reason = "stripe_deleted"


def handle_charge_refunded(obj: dict) -> None:
    """Refunds reverse the agency share on the net amount."""
    charge_id = obj.get("id")
    payment = (
        db.session.query(Payment).filter_by(stripe_charge_id=charge_id).one_or_none()
    )
    if payment is None and obj.get("invoice"):
        payment = (
            db.session.query(Payment)
            .filter_by(stripe_invoice_id=obj["invoice"])
            .one_or_none()
        )
    if payment is None:
        return
    payment.refunded_cents = obj.get("amount_refunded", 0)
    client = db.session.get(ClientAccount, payment.client_account_id)
    net = max(0, payment.amount_cents - payment.refunded_cents)
    # share is on the pre-tax slice of what was kept: scale net by the
    # invoice's pre-tax fraction so refunded GST never inflates the share
    pretax_fraction = (
        (payment.amount_cents - (payment.tax_cents or 0)) / payment.amount_cents
        if payment.amount_cents
        else 0
    )
    payment.agency_share_cents = round(net * pretax_fraction * client.commission_rate)
    if payment.refunded_cents >= payment.amount_cents:
        payment.status = "refunded"


def _send_welcome(sub: Subscription) -> None:
    from .signed_links import SALT_SET_PASSWORD

    attendee = db.session.get(AttendeeProfile, sub.attendee_id)
    guardian = attendee.guardian
    is_child = attendee.kind == AttendeeKind.child.value
    # Member account invite: set-password link into the portal
    invite_url = absolute_url(
        "portal.set_password", token=make_token(guardian.id, SALT_SET_PASSWORD)
    )
    guardian.invited_at = utcnow()
    # Family-pricing nudge: what the NEXT family member would save. Counts
    # this guardian's live subs; the discount applies automatically.
    plan = db.session.get(Plan, sub.plan_id)
    live_subs = (
        db.session.query(Subscription)
        .filter(
            Subscription.user_id == guardian.id,
            Subscription.status.in_(
                [
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]
            ),
        )
        .count()
    )
    from .tax import fmt_cents

    next_family_discount = None
    if plan:
        saving = plan.price_cents - family_price_cents(live_subs, plan)
        if saving > 0:
            next_family_discount = fmt_cents(saving)
    html = render_template(
        "emails/membership_welcome.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        cohort=sub.cohort_label,
        invite_url=invite_url,
        next_family_discount=next_family_discount,
    )
    who = f"{attendee.first_name} is" if is_child else "You're"
    send_email(
        guardian, guardian.email,
        f"{who} officially a Box2Fit member!",
        html, "membership_welcome", sub.client_account_id,
        attendee_id=attendee.id,
    )


def _send_recovered(sub: Subscription) -> None:
    guardian = db.session.get(User, sub.user_id)
    send_email(
        guardian, guardian.email,
        "All sorted — thanks for updating your card",
        render_template("emails/payment_recovered.html", guardian=guardian),
        "payment_recovered", sub.client_account_id,
    )
