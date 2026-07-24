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


def staff_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role not in STAFF_ROLES:
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
            return redirect(url_for("ops.today"))
        flash("Invalid email or password.", "error")
    return render_template("ops/login.html", form=form)


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("ops.login"))


@bp.get("/")
@bp.get("/today")
@staff_required
def today():
    d = today_local()
    instances = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.client_account_id == current_user.client_account_id,
            ClassInstance.local_date == d,
            ClassInstance.status == InstanceStatus.scheduled.value,
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
    return render_template(
        "ops/today.html",
        instances=instances,
        counts=counts,
        rosters=rosters,
        today=d,
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
        if booking.kind in ("trial", "walkin"):
            _send_post_class_email(booking)
    elif action == "no_show":
        booking.status = BookingStatus.no_show.value
    elif action == "undo":
        booking.status = BookingStatus.booked.value
        booking.checked_in_at = None
    else:
        abort(400)
    booking.attendance_marked_by = current_user.email
    db.session.commit()
    return redirect(url_for("ops.today"))


@bp.get("/kiosk")
@staff_required
def kiosk():
    """Door tablet: big search, tap to check in, huge green confirmation.
    Operable by a sweaty person in seven seconds."""
    return render_template("ops/kiosk.html", result=None, query="")


@bp.post("/kiosk/search")
@staff_required
def kiosk_search():
    from ..models import ClassInstance, InstanceStatus
    from ..services.tzutil import today_local

    query = (request.form.get("q") or "").strip().lower()
    matches = []
    if len(query) >= 2:
        rows = (
            db.session.query(Booking)
            .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
            .filter(
                Booking.client_account_id == current_user.client_account_id,
                ClassInstance.local_date == today_local(),
                ClassInstance.status == InstanceStatus.scheduled.value,
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
@staff_required
def kiosk_checkin(booking_id: int):
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.client_account_id != current_user.client_account_id:
        abort(404)
    first_time = booking.kind in ("trial", "walkin") and booking.checked_in_at is None
    if booking.status != BookingStatus.attended.value:
        booking.status = BookingStatus.attended.value
        booking.checked_in_at = utcnow()
        booking.attendance_marked_by = "kiosk"
        if booking.kind in ("trial", "walkin"):
            _send_post_class_email(booking)
        db.session.commit()
    return render_template(
        "ops/kiosk.html",
        result={"attendee": booking.attendee, "first_time": first_time},
        query="",
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
