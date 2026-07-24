"""JSON API (JWT). Every member-facing action lands here so the Phase 2
mobile app reuses it. Pass 1: auth token issuance, profile, class listing
with live availability. Booking/portal endpoints grow in Pass 2/3."""
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Blueprint, current_app, g, jsonify, request

from ..extensions import db
from ..models import ClientAccount, User
from ..services.scheduling import upcoming_instances
from ..services.tzutil import utc_to_local

bp = Blueprint("api", __name__)


def issue_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "client_account_id": user.client_account_id,
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=current_app.config["JWT_EXPIRES_MINUTES"]),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET"], algorithm="HS256")


def jwt_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify(error="missing_token"), 401
        try:
            payload = jwt.decode(
                header[7:], current_app.config["JWT_SECRET"], algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            return jsonify(error="invalid_token"), 401
        user = db.session.get(User, int(payload["sub"]))
        if user is None or not user.active:
            return jsonify(error="invalid_token"), 401
        g.api_user = user
        return fn(*args, **kwargs)

    return wrapper


@bp.get("/health")
def health():
    return {"status": "ok", "version": 1}


@bp.post("/webhooks/stripe")
def stripe_webhook():
    """Stripe webhook receiver. Signature-verified when a webhook secret is
    configured; in dev/test (no secret) the JSON body is trusted as-is."""
    from ..services import billing

    secret = current_app.config["STRIPE_WEBHOOK_SECRET"]
    payload = request.get_data()
    if secret:
        import stripe

        try:
            event = stripe.Webhook.construct_event(
                payload, request.headers.get("Stripe-Signature", ""), secret
            )
        except Exception:
            return jsonify(error="invalid_signature"), 400
        event_type = event["type"]
        obj = event["data"]["object"]
    else:
        body = request.get_json(silent=True) or {}
        event_type = body.get("type", "")
        obj = (body.get("data") or {}).get("object") or {}

    handlers = {
        "setup_intent.succeeded": billing.handle_setup_intent_succeeded,
        "invoice.paid": billing.handle_invoice_paid,
        "invoice.payment_failed": billing.handle_invoice_payment_failed,
        "customer.subscription.deleted": billing.handle_subscription_deleted,
        "charge.refunded": billing.handle_charge_refunded,
    }
    handler = handlers.get(event_type)
    if handler:
        handler(obj)
        db.session.commit()
    return jsonify(received=True)


@bp.post("/auth/token")
def auth_token():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = db.session.query(User).filter_by(email=email, active=True).first()
    if user is None or not user.check_password(password):
        return jsonify(error="invalid_credentials"), 401
    return jsonify(token=issue_token(user))


@bp.get("/me")
@jwt_required
def me():
    u = g.api_user
    return jsonify(
        id=u.id,
        name=u.name,
        email=u.email,
        role=u.role,
        attendees=[
            {
                "id": a.id,
                "kind": a.kind,
                "first_name": a.first_name,
                "birth_year": a.birth_year,
            }
            for a in u.attendees
            if a.active
        ],
    )


@bp.post("/webhooks/calls")
def calls_webhook():
    """CallRail-compatible call-tracking webhook."""
    from ..models import ClientAccount
    from ..services import calls

    client = (
        db.session.query(ClientAccount).filter_by(active=True).order_by(
            ClientAccount.id
        ).first()
    )
    if client is None:
        return jsonify(error="no_client"), 503
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    call = calls.ingest(client.id, payload)
    db.session.commit()
    return jsonify(received=True, call_id=call.id, matched=call.matched_lead_id)


@bp.post("/bookings")
@jwt_required
def api_create_booking():
    """Member booking (the same RSVP action the portal uses; the Phase 2 app
    calls this)."""
    from ..models import (
        AttendeeProfile,
        Booking,
        BookingKind,
        BookingStatus,
        ClassInstance,
        Subscription,
        SubscriptionStatus,
    )
    from ..services import waitlist
    from ..services.scheduling import validate_bookable

    data = request.get_json(silent=True) or {}
    attendee = db.session.get(AttendeeProfile, data.get("attendee_id") or 0)
    instance = db.session.get(ClassInstance, data.get("instance_id") or 0)
    if attendee is None or attendee.user_id != g.api_user.id or instance is None:
        return jsonify(error="not_found"), 404

    sub = (
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
    if (
        sub
        and sub.cohort_label
        and instance.cohort_label
        and instance.cohort_label != sub.cohort_label
        and not current_app.config["POLICY_ALLOW_CROSS_GROUP"]
    ):
        return jsonify(error="wrong_group", group=sub.cohort_label), 409

    err = validate_bookable(instance, attendee=attendee, for_trial=sub is None)
    if err == "That class is full.":
        entry = waitlist.join(instance, attendee)
        db.session.commit()
        return jsonify(waitlisted=True, position=entry.position), 202
    if err:
        return jsonify(error=err), 409

    existing = (
        db.session.query(Booking)
        .filter_by(attendee_id=attendee.id, class_instance_id=instance.id)
        .one_or_none()
    )
    if existing:
        existing.status = BookingStatus.booked.value
        existing.cancelled_at = None
        booking = existing
    else:
        booking = Booking(
            client_account_id=instance.client_account_id,
            attendee_id=attendee.id,
            class_instance_id=instance.id,
            kind=BookingKind.member.value if sub else BookingKind.trial.value,
        )
        db.session.add(booking)
    db.session.commit()
    return jsonify(booked=True, booking_id=booking.id), 201


@bp.post("/bookings/<int:booking_id>/cancel")
@jwt_required
def api_cancel_booking(booking_id: int):
    from ..models import Booking, BookingStatus, utcnow
    from ..services import waitlist
    from ..services.tzutil import now_utc

    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.attendee.user_id != g.api_user.id:
        return jsonify(error="not_found"), 404
    if booking.status == BookingStatus.booked.value:
        booking.status = BookingStatus.cancelled.value
        booking.cancelled_at = utcnow()
        late_hours = current_app.config["POLICY_LATE_CANCEL_HOURS"]
        seconds_left = (
            booking.class_instance.starts_at_utc - now_utc()
        ).total_seconds()
        booking.late_cancel = 0 < seconds_left < late_hours * 3600
        waitlist.promote_next(booking.class_instance)
        db.session.commit()
    return jsonify(cancelled=True)


@bp.get("/me/bookings")
@jwt_required
def api_my_bookings():
    from ..models import Booking

    ids = [a.id for a in g.api_user.attendees] or [0]
    rows = (
        db.session.query(Booking)
        .filter(Booking.attendee_id.in_(ids))
        .order_by(Booking.id.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        bookings=[
            {
                "id": b.id,
                "attendee_id": b.attendee_id,
                "class_type": b.class_instance.class_type.name,
                "cohort": b.class_instance.cohort_label,
                "starts_at_local": utc_to_local(
                    b.class_instance.starts_at_utc
                ).isoformat(),
                "status": b.status,
            }
            for b in rows
        ]
    )


@bp.get("/classes")
def classes():
    """Public upcoming schedule with live availability. Filters: segment,
    class_type_id, days, trials_only."""
    client = (
        db.session.query(ClientAccount).filter_by(active=True).order_by(
            ClientAccount.id
        ).first()
    )
    if client is None:
        return jsonify(classes=[])
    occ = upcoming_instances(
        client.id,
        segment_tag=request.args.get("segment"),
        class_type_id=request.args.get("class_type_id", type=int),
        days=min(request.args.get("days", 14, type=int), 28),
        trials_only=request.args.get("trials_only") == "1",
    )
    return jsonify(
        classes=[
            {
                "instance_id": o["instance"].id,
                "class_type": o["instance"].class_type.name,
                "cohort": o["instance"].cohort_label,
                "age_bracket": o["instance"].class_type.age_bracket_label(),
                "starts_at_local": utc_to_local(
                    o["instance"].starts_at_utc
                ).isoformat(),
                "duration_min": o["instance"].duration_min,
                "trainer": o["instance"].trainer.name
                if o["instance"].trainer
                else None,
                "capacity": o["instance"].capacity,
                "remaining": o["remaining"],
                "accepts_trials": o["instance"].accepts_trials,
            }
            for o in occ
        ]
    )
