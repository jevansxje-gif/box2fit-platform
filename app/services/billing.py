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
                   "month": {"interval": "month", "interval_count": 1}}


class ActivationError(Exception):
    pass


def default_plan(client_account_id: int) -> Plan | None:
    return (
        db.session.query(Plan)
        .filter_by(client_account_id=client_account_id, active=True)
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


def activate_subscription(
    attendee: AttendeeProfile,
    plan: Plan | None = None,
    cohort_label: str | None = None,
    actor: str = "member",
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

    sub = Subscription(
        client_account_id=attendee.client_account_id,
        user_id=guardian.id,
        attendee_id=attendee.id,
        plan_id=plan.id,
        cohort_label=cohort_label,
        status=SubscriptionStatus.pending.value,
        mrr_cents=plan.price_cents,
        first_charge_at=first_charge_at,
    )
    db.session.add(sub)
    db.session.flush()

    if stripe_service.is_configured():
        stripe = stripe_service.stripe_client()
        price_id = ensure_stripe_price(plan)
        ssub = stripe.Subscription.create(
            customer=customer.stripe_customer_id,
            items=[{"price": price_id}],
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
    price = f"${plan.price_cents // 100}" if plan.price_cents % 100 == 0 else f"${plan.price_cents / 100:.2f}"
    lead_hours = current_app.config["PRE_CHARGE_LEAD_HOURS"]
    is_child = attendee.kind == AttendeeKind.child.value
    html = render_template(
        "emails/pre_charge_reminder.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        price=price,
        lead_hours=lead_hours,
        cohort=sub.cohort_label,
        cancel_url=cancel_url,
    )
    who = f"{attendee.first_name}'s" if is_child else "Your"
    send_email(
        guardian, guardian.email,
        f"{who} Box2Fit membership starts in {lead_hours} hours",
        html, "pre_charge_reminder", sub.client_account_id,
        attendee_id=attendee.id,
    )
    send_sms(
        guardian, guardian.phone,
        f"Box2Fit: {who.lower()} membership ({price} every 4 weeks) starts in "
        f"{lead_hours} hours. Cancel anytime in one click: {cancel_url}",
        "pre_charge_reminder", sub.client_account_id, attendee_id=attendee.id,
    )
    sub.pre_charge_reminder_sent_at = utcnow()


def cancel_subscription(sub: Subscription, reason: str, note: str | None = None) -> None:
    """Cancel locally + on Stripe. Used by cancel-before-charge (no charge
    ever happens) and later by the portal cancel flow."""
    if sub.status == SubscriptionStatus.cancelled.value:
        return
    if sub.stripe_subscription_id and stripe_service.is_configured():
        stripe = stripe_service.stripe_client()
        try:
            stripe.Subscription.cancel(sub.stripe_subscription_id)
        except Exception:
            log.exception("stripe subscription cancel failed (id=%s)", sub.id)
    sub.status = SubscriptionStatus.cancelled.value
    sub.cancelled_at = utcnow()
    sub.cancel_reason = reason
    sub.cancel_reason_note = note


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
    if not already_vaulted:
        guardian = db.session.get(User, customer.user_id)
        enqueue_event(
            "AddPaymentInfo", guardian.client_account_id, _lead_for(guardian)
        )


def handle_invoice_paid(obj: dict) -> Payment | None:
    invoice_id = obj.get("id")
    sub_id = obj.get("subscription")
    amount = obj.get("amount_paid", 0)
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
    existing = (
        db.session.query(Payment).filter_by(stripe_invoice_id=invoice_id).one_or_none()
    )
    if existing:
        return existing  # idempotent on redelivery

    client = db.session.get(ClientAccount, sub.client_account_id)
    payment = Payment(
        client_account_id=sub.client_account_id,
        user_id=sub.user_id,
        subscription_id=sub.id,
        stripe_invoice_id=invoice_id,
        stripe_charge_id=obj.get("charge"),
        amount_cents=amount,
        currency=(obj.get("currency") or "cad").upper(),
        status="paid",
        agency_share_cents=round(amount * client.commission_rate),
        paid_at=utcnow(),
    )
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
        _send_welcome(sub)
    elif was_past_due:
        _send_recovered(sub)
    return payment


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
    payment.agency_share_cents = round(net * client.commission_rate)
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
    html = render_template(
        "emails/membership_welcome.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        cohort=sub.cohort_label,
        invite_url=invite_url,
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
