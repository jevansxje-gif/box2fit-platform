"""Gym operations backend. Pass 1: staff login + the minimal Today view —
today's classes, rosters (child name + guardian contact), attendance marking,
walk-in shortcut."""
import logging
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import (
    Booking,
    BookingStatus,
    ClassInstance,
    InstanceStatus,
    Role,
    User,
    utcnow,
)
from ..services.scheduling import booked_counts
from ..services.tzutil import today_local
from .forms import OpsLoginForm

bp = Blueprint("ops", __name__)
log = logging.getLogger(__name__)

STAFF_ROLES = {
    Role.front_desk.value,
    Role.trainer.value,
    Role.gym_admin.value,
    Role.agency_admin.value,
}
# Coaches get their own view + attendance marking; the rest of the back
# office is desk/admin territory.
DESK_ROLES = {
    Role.front_desk.value,
    Role.gym_admin.value,
    Role.agency_admin.value,
}


def staff_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role not in STAFF_ROLES:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def desk_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role not in DESK_ROLES:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    form = OpsLoginForm()
    if form.validate_on_submit():
        user = (
            db.session.query(User)
            .filter_by(email=form.email.data.strip().lower(), active=True)
            .first()
        )
        if user and user.role in STAFF_ROLES and user.check_password(form.password.data):
            login_user(user)
            if user.role == Role.trainer.value:
                return redirect(url_for("ops.coach"))
            return redirect(url_for("ops.today"))
        flash("Invalid email or password.", "error")
    return render_template("ops/login.html", form=form)


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("ops.login"))


SIGNUP_WINDOW_DAYS = 7  # how far back the Today view's sign-ups panel looks


@bp.get("/coach")
@staff_required
def coach():
    """The coach's phone view: today's classes expanded with big attendance
    buttons (the door gate — no tablet needed), plus the next week of their
    classes collapsed under Upcoming."""
    from datetime import timedelta

    from sqlalchemy import or_

    from ..models import ScheduleTemplate, Trainer

    d = today_local()
    me = (
        db.session.query(Trainer)
        .filter(
            Trainer.client_account_id == current_user.client_account_id,
            or_(Trainer.user_id == current_user.id, Trainer.email == current_user.email),
        )
        .first()
    )
    instances = (
        db.session.query(ClassInstance)
        .outerjoin(ScheduleTemplate, ClassInstance.template_id == ScheduleTemplate.id)
        .filter(
            ClassInstance.client_account_id == current_user.client_account_id,
            ClassInstance.local_date >= d,
            ClassInstance.local_date <= d + timedelta(days=7),
            ClassInstance.status == InstanceStatus.scheduled.value,
            or_(
                ClassInstance.template_id.is_(None),
                ScheduleTemplate.active.is_(True),
            ),
        )
        .order_by(ClassInstance.local_date, ClassInstance.local_time)
        .all()
    )
    mine = [i for i in instances if me and i.trainer_id == me.id]
    shown = mine if mine else instances
    today_instances = [i for i in shown if i.local_date == d]
    upcoming: list[tuple] = []  # [(date, [instances]), ...] in order
    for inst in shown:
        if inst.local_date == d:
            continue
        if upcoming and upcoming[-1][0] == inst.local_date:
            upcoming[-1][1].append(inst)
        else:
            upcoming.append((inst.local_date, [inst]))
    rosters = {
        inst.id: [
            b
            for b in inst.bookings
            if b.status
            in (
                BookingStatus.booked.value,
                BookingStatus.attended.value,
                BookingStatus.no_show.value,
            )
        ]
        for inst in shown
    }
    return render_template(
        "ops/coach.html",
        instances=today_instances,
        upcoming=upcoming,
        rosters=rosters,
        today=d,
        me=me,
        showing_all=not mine,
    )


@bp.get("/")
@bp.get("/today")
@desk_required
def today():
    from datetime import timedelta

    from ..models import BookingKind, utcnow

    from sqlalchemy import or_

    from ..models import ScheduleTemplate

    d = today_local()
    recent_signups = (
        db.session.query(Booking)
        .filter(
            Booking.client_account_id == current_user.client_account_id,
            Booking.kind.in_([BookingKind.trial.value, BookingKind.walkin.value]),
            Booking.booked_at >= utcnow() - timedelta(days=SIGNUP_WINDOW_DAYS),
        )
        .order_by(Booking.booked_at.desc())
        .limit(25)
        .all()
    )
    instances = (
        db.session.query(ClassInstance)
        .outerjoin(ScheduleTemplate, ClassInstance.template_id == ScheduleTemplate.id)
        .filter(
            ClassInstance.client_account_id == current_user.client_account_id,
            ClassInstance.local_date == d,
            ClassInstance.status == InstanceStatus.scheduled.value,
            # deactivated weekly templates never show, even for legacy
            # instances generated before the deactivation
            or_(
                ClassInstance.template_id.is_(None),
                ScheduleTemplate.active.is_(True),
            ),
        )
        .order_by(ClassInstance.local_time)
        .all()
    )
    counts = booked_counts([i.id for i in instances])
    rosters = {}
    for inst in instances:
        rosters[inst.id] = [
            b
            for b in inst.bookings
            if b.status
            in (
                BookingStatus.booked.value,
                BookingStatus.attended.value,
                BookingStatus.no_show.value,
            )
        ]

    # Trial follow-ups: attended a free class in the last week, no
    # membership yet — the front desk's call list. (A day-2 email nudge
    # goes out automatically; a voice closes fence-sitters.)
    from ..models import Subscription, SubscriptionStatus

    live_attendee_ids = {
        s.attendee_id
        for s in db.session.query(Subscription).filter(
            Subscription.client_account_id == current_user.client_account_id,
            Subscription.status.in_(
                [
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]
            ),
        )
    }
    from datetime import timedelta as _td

    from ..models import BookingKind as _BK

    follow_ups = [
        b
        for b in db.session.query(Booking)
        .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
        .filter(
            Booking.client_account_id == current_user.client_account_id,
            Booking.kind.in_([_BK.trial.value, _BK.walkin.value]),
            Booking.status == BookingStatus.attended.value,
            ClassInstance.local_date >= d - _td(days=7),
            ClassInstance.local_date < d,
        )
        .order_by(ClassInstance.local_date)
        .all()
        if b.attendee_id not in live_attendee_ids
    ]
    return render_template(
        "ops/today.html",
        instances=instances,
        counts=counts,
        rosters=rosters,
        today=d,
        recent_signups=recent_signups,
        signup_window_days=SIGNUP_WINDOW_DAYS,
        follow_ups=follow_ups,
    )


@bp.post("/bookings/<int:booking_id>/attendance")
@staff_required
def mark_attendance(booking_id: int):
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.client_account_id != current_user.client_account_id:
        abort(404)
    action = request.form.get("action")
    if action == "attended":
        booking.status = BookingStatus.attended.value
        booking.checked_in_at = utcnow()
        booking.attendance_marked_by = current_user.email
        db.session.commit()
        if booking.kind in ("trial", "walkin"):
            _post_class_followup(booking)
        return redirect(url_for(_attendance_return_endpoint()))
    elif action == "no_show":
        booking.status = BookingStatus.no_show.value
    elif action == "undo":
        booking.status = BookingStatus.booked.value
        booking.checked_in_at = None
    else:
        abort(400)
    booking.attendance_marked_by = current_user.email
    db.session.commit()
    return redirect(url_for(_attendance_return_endpoint()))


def _attendance_return_endpoint() -> str:
    """Coaches bounce back to their own view after marking attendance."""
    if current_user.role == Role.trainer.value:
        return "ops.coach"
    return "ops.today"


@bp.get("/kiosk")
@desk_required
def kiosk():
    """Door tablet: big search, tap to check in, huge green confirmation.
    Operable by a sweaty person in seven seconds."""
    return render_template("ops/kiosk.html", result=None, query="")


@bp.post("/kiosk/search")
@desk_required
def kiosk_search():
    from ..models import ClassInstance, InstanceStatus
    from ..services.tzutil import today_local

    from sqlalchemy import or_

    from ..models import ScheduleTemplate

    query = (request.form.get("q") or "").strip().lower()
    matches = []
    if len(query) >= 2:
        rows = (
            db.session.query(Booking)
            .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
            .outerjoin(
                ScheduleTemplate, ClassInstance.template_id == ScheduleTemplate.id
            )
            .filter(
                Booking.client_account_id == current_user.client_account_id,
                ClassInstance.local_date == today_local(),
                ClassInstance.status == InstanceStatus.scheduled.value,
                or_(
                    ClassInstance.template_id.is_(None),
                    ScheduleTemplate.active.is_(True),
                ),
                Booking.status.in_(
                    [BookingStatus.booked.value, BookingStatus.attended.value]
                ),
            )
            .all()
        )
        for b in rows:
            name = f"{b.attendee.first_name} {b.attendee.last_name or ''}".lower()
            guardian = b.attendee.guardian.name.lower()
            if query in name or query in guardian:
                matches.append(b)
    return render_template("ops/kiosk.html", matches=matches, query=query, result=None)


@bp.post("/kiosk/checkin/<int:booking_id>")
@desk_required
def kiosk_checkin(booking_id: int):
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.client_account_id != current_user.client_account_id:
        abort(404)
    first_time = booking.kind in ("trial", "walkin") and booking.checked_in_at is None
    if booking.status != BookingStatus.attended.value:
        booking.status = BookingStatus.attended.value
        booking.checked_in_at = utcnow()
        booking.attendance_marked_by = "kiosk"
        db.session.commit()
        if booking.kind in ("trial", "walkin"):
            _post_class_followup(booking)
    return render_template(
        "ops/kiosk.html",
        result={"attendee": booking.attendee, "first_time": first_time},
        query="",
    )


def _post_class_followup(booking: Booking) -> None:
    """Auto-start policy (client decision 2026-08-27): a trial attendance
    starts the membership automatically on the vaulted card — the pre-charge
    reminder (48h, one-click cancel) is the member's notice, matching the
    disclosure they agreed to at booking. Falls back to the 'Loved it?'
    activation email when no card is on file. Never breaks attendance
    marking."""
    from flask import flash

    from ..models import (
        PaymentMethodStatus,
        StripeCustomer,
        Subscription,
        SubscriptionStatus,
    )
    from ..services import billing

    attendee = booking.attendee
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
        return  # already a member — nothing to start, nothing to email

    customer = (
        db.session.query(StripeCustomer)
        .filter_by(user_id=attendee.user_id)
        .one_or_none()
    )
    vaulted = (
        customer is not None
        and customer.payment_method_status == PaymentMethodStatus.vaulted.value
    )
    if vaulted:
        try:
            billing.activate_subscription(
                attendee,
                cohort_label=booking.class_instance.cohort_label,
                actor="auto_post_class",
            )
            db.session.commit()
            flash(
                f"{attendee.first_name} attended — membership starts "
                "automatically in 48 hours (reminder with one-click cancel sent).",
                "success",
            )
            return
        except billing.ActivationError:
            db.session.rollback()
        except Exception:
            db.session.rollback()
            import logging

            logging.getLogger(__name__).exception(
                "auto-activation failed for booking %s", booking.id
            )
    # fallback: no card (or activation failed) → the activate-link email
    _send_post_class_email(booking)
    db.session.commit()
    flash(
        f"{attendee.first_name} attended — no card on file for auto-start, "
        "sent the membership email instead.",
        "success",
    )


def _send_post_class_email(booking: Booking) -> None:
    """'Loved it?' email after a trial attendance: activate or one-click
    cancel, both signed links, no login needed."""
    from flask import render_template

    from ..services.messaging import send_email
    from ..services.signed_links import SALT_ACTIVATE, SALT_CANCEL_BOOKING, make_token
    from ..services.urls import absolute_url

    attendee = booking.attendee
    guardian = attendee.guardian
    activate_url = absolute_url(
        "funnel.activate_membership", token=make_token(booking.id, SALT_ACTIVATE)
    )
    html = render_template(
        "emails/post_class.html",
        guardian=guardian,
        attendee=attendee,
        is_child=attendee.kind == "child",
        activate_url=activate_url,
    )
    who = f"How was {attendee.first_name}'s" if attendee.kind == "child" else "How was your"
    send_email(
        guardian, guardian.email, f"{who} first class?", html,
        "post_class", booking.client_account_id, attendee_id=attendee.id,
    )


@bp.post("/bookings/<int:booking_id>/activate")
@staff_required
def activate_membership(booking_id: int):
    """Front-desk activation after the class ('sign me up' in person)."""
    from ..services import billing

    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.client_account_id != current_user.client_account_id:
        abort(404)
    try:
        billing.activate_subscription(
            booking.attendee,
            cohort_label=booking.class_instance.cohort_label,
            actor=current_user.email,
        )
        db.session.commit()
        flash(
            f"Membership activated for {booking.attendee.first_name} — first "
            "charge in 48 hours, reminder sent.",
            "success",
        )
    except billing.ActivationError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("ops.today"))
