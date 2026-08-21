"""Guardian/member portal (Pass 3).

Auth is guardian-friendly: password login, magic-link login (trial bookers
have accounts but often no password yet), and set-password via the invite
link sent at activation. The card-update page stays reachable by signed link
alone (dunning emails) — a past-due guardian may never have logged in.
"""
import logging
import re
from datetime import date, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, limiter
from ..models import (
    AttendeeProfile,
    Booking,
    BookingKind,
    BookingStatus,
    ClassInstance,
    Payment,
    Plan,
    Role,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
    WaiverDocument,
    WaiverSignature,
    WaitlistEntry,
    utcnow,
)
from ..services import stripe_service, waitlist
from ..services.booking_flow import sign_waiver
from ..services.messaging import send_email
from ..services.scheduling import booked_counts, upcoming_instances, validate_bookable
from ..services.signed_links import (
    SALT_MAGIC_LOGIN,
    SALT_SET_PASSWORD,
    SALT_UPDATE_CARD,
    SALT_WAITLIST_CONFIRM,
    make_token,
    read_token,
)
from ..services.tzutil import now_utc, today_local, utc_to_local
from ..services.urls import absolute_url

bp = Blueprint("portal", __name__)
log = logging.getLogger(__name__)

MAGIC_MAX_AGE = 30 * 60  # magic links live 30 minutes


def _home_url(user) -> str:
    """Where a freshly signed-in user belongs. Staff accounts (coach invites
    reuse the member set-password flow) go to their ops home, not the portal."""
    from .ops import STAFF_ROLES

    if user.role == Role.trainer.value:
        return url_for("ops.coach")
    if user.role in STAFF_ROLES:
        return url_for("ops.today")
    return url_for("portal.dashboard")


# ------------------------------------------------------------------- auth ---
@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20/hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("portal.dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        mode = request.form.get("mode", "password")
        user = db.session.query(User).filter_by(email=email, active=True).first()
        if mode == "magic":
            # Same response whether or not the account exists (no enumeration)
            if user:
                url = absolute_url(
                    "portal.magic_login", token=make_token(user.id, SALT_MAGIC_LOGIN)
                )
                send_email(
                    user, user.email, "Your Box2Fit sign-in link",
                    render_template("emails/magic_link.html", user=user, url=url),
                    "magic_link", user.client_account_id,
                )
                db.session.commit()
            flash("If that email has an account, a sign-in link is on its way.", "success")
        elif user and user.check_password(password):
            login_user(user)
            return redirect(_home_url(user))
        else:
            flash("Invalid email or password. No password yet? Use the email link option.", "error")
    return render_template("portal/login.html")


@bp.get("/magic/<token>")
def magic_login(token: str):
    user_id = read_token(token, SALT_MAGIC_LOGIN, max_age=MAGIC_MAX_AGE)
    if user_id is None:
        flash("That sign-in link has expired — request a fresh one.", "error")
        return redirect(url_for("portal.login"))
    user = db.session.get(User, user_id)
    if user is None or not user.active:
        abort(404)
    login_user(user)
    return redirect(_home_url(user))


@bp.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token: str):
    user_id = read_token(token, SALT_SET_PASSWORD)
    if user_id is None:
        abort(404)
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        pw = request.form.get("password") or ""
        if len(pw) < 8:
            flash("Password needs at least 8 characters.", "error")
        else:
            user.set_password(pw)
            db.session.commit()
            login_user(user)
            flash("Password set — welcome!", "success")
            return redirect(_home_url(user))
    return render_template("portal/set_password.html", user=user)


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("portal.login"))


# -------------------------------------------------------------- dashboard ---
def _my_attendees() -> list[AttendeeProfile]:
    return [a for a in current_user.attendees if a.active]


def _my_subscriptions() -> list[Subscription]:
    return (
        db.session.query(Subscription)
        .filter(
            Subscription.user_id == current_user.id,
            Subscription.status.in_(
                [
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]
            ),
        )
        .all()
    )


def _ensure_referral_code(user: User) -> str:
    if not user.referral_code:
        base = re.sub(r"[^A-Z]", "", user.name.split(" ")[0].upper())[:8] or "B2F"
        user.referral_code = f"{base}-{user.id}"
        db.session.commit()
    return user.referral_code


@bp.get("/")
@login_required
def dashboard():
    if current_user.role != Role.member.value:
        return redirect(_home_url(current_user))
    attendees = _my_attendees()
    subs = _my_subscriptions()
    sub_by_attendee = {s.attendee_id: s for s in subs}

    next_booking = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(
            Booking.attendee_id.in_([a.id for a in attendees] or [0]),
            Booking.status == BookingStatus.booked.value,
            ClassInstance.starts_at_utc > now_utc(),
        )
        .order_by(ClassInstance.starts_at_utc)
        .first()
    )
    cohorts = {s.cohort_label for s in subs if s.cohort_label}
    quick = [
        o
        for o in upcoming_instances(current_user.client_account_id, days=10)
        if not cohorts or o["instance"].cohort_label in cohorts
    ][:3]
    past_due = any(s.status == SubscriptionStatus.past_due.value for s in subs)
    from ..models import Announcement, Booking as B  # noqa: F401

    announcements = (
        db.session.query(Announcement)
        .filter_by(client_account_id=current_user.client_account_id, active=True)
        .order_by(Announcement.created_at.desc())
        .limit(3)
        .all()
    )
    return render_template(
        "portal/dashboard.html",
        announcements=announcements,
        attendees=attendees,
        sub_by_attendee=sub_by_attendee,
        next_booking=next_booking,
        quick=quick,
        past_due=past_due,
        referral_code=_ensure_referral_code(current_user),
        card_token=make_token(current_user.id, SALT_UPDATE_CARD),
    )


# ------------------------------------------------- schedule + RSVP booking ---
@bp.get("/schedule")
@login_required
def schedule():
    attendees = _my_attendees()
    subs = _my_subscriptions()
    sub_by_attendee = {s.attendee_id: s for s in subs}
    occurrences = upcoming_instances(current_user.client_account_id, days=14)

    # Members are locked to their group unless the policy allows crossing.
    if not current_app.config["POLICY_ALLOW_CROSS_GROUP"]:
        cohorts = {s.cohort_label for s in subs if s.cohort_label}
        if cohorts:
            occurrences = [
                o
                for o in occurrences
                if o["instance"].cohort_label is None
                or o["instance"].cohort_label in cohorts
            ]

    ids = [a.id for a in attendees] or [0]
    my_bookings = (
        db.session.query(Booking)
        .filter(
            Booking.attendee_id.in_(ids),
            Booking.status == BookingStatus.booked.value,
        )
        .all()
    )
    booked_map = {(b.attendee_id, b.class_instance_id): b for b in my_bookings}
    waiting = {
        (w.attendee_id, w.class_instance_id): w
        for w in db.session.query(WaitlistEntry)
        .filter(
            WaitlistEntry.attendee_id.in_(ids),
            WaitlistEntry.status.in_(["waiting", "offered"]),
        )
        .all()
    }
    return render_template(
        "portal/schedule.html",
        attendees=attendees,
        occurrences=occurrences,
        booked_map=booked_map,
        waiting=waiting,
        past_due=any(
            s.status == SubscriptionStatus.past_due.value for s in subs
        ),
        card_token=make_token(current_user.id, SALT_UPDATE_CARD),
    )


def _own_attendee(attendee_id: int) -> AttendeeProfile:
    attendee = db.session.get(AttendeeProfile, attendee_id)
    if attendee is None or attendee.user_id != current_user.id:
        abort(404)
    return attendee


@bp.post("/bookings")
@login_required
def create_booking():
    attendee = _own_attendee(request.form.get("attendee_id", type=int) or 0)
    instance = db.session.get(ClassInstance, request.form.get("instance_id", type=int) or 0)
    if instance is None:
        abort(404)

    subs = {s.attendee_id: s for s in _my_subscriptions()}
    sub = subs.get(attendee.id)
    if (
        sub
        and sub.cohort_label
        and instance.cohort_label
        and instance.cohort_label != sub.cohort_label
        and not current_app.config["POLICY_ALLOW_CROSS_GROUP"]
    ):
        flash(
            f"{attendee.first_name} is enrolled in {sub.cohort_label} — "
            f"{instance.cohort_label} days aren't bookable on this membership.",
            "error",
        )
        return redirect(url_for("portal.schedule"))

    err = validate_bookable(instance, attendee=attendee, for_trial=sub is None)
    if err == "That class is full.":
        waitlist.join(instance, attendee)
        db.session.commit()
        flash(
            f"That class is full — {attendee.first_name} joined the waitlist. "
            "If a spot opens you'll be booked automatically and notified.",
            "success",
        )
        return redirect(url_for("portal.schedule"))
    if err:
        flash(err, "error")
        return redirect(url_for("portal.schedule"))

    existing = (
        db.session.query(Booking)
        .filter_by(attendee_id=attendee.id, class_instance_id=instance.id)
        .one_or_none()
    )
    if existing:
        existing.status = BookingStatus.booked.value
        existing.cancelled_at = None
    else:
        db.session.add(
            Booking(
                client_account_id=instance.client_account_id,
                attendee_id=attendee.id,
                class_instance_id=instance.id,
                kind=BookingKind.member.value if sub else BookingKind.trial.value,
            )
        )
    db.session.commit()
    flash(f"{attendee.first_name} is booked — see you there!", "success")
    return redirect(url_for("portal.schedule"))


@bp.post("/bookings/<int:booking_id>/cancel")
@login_required
def cancel_booking(booking_id: int):
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.attendee.user_id != current_user.id:
        abort(404)
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
        flash("Booking cancelled — the spot has been released.", "success")
    return redirect(url_for("portal.schedule"))


@bp.get("/waitlist/confirm/<token>")
def waitlist_confirm(token: str):
    entry_id = read_token(token, SALT_WAITLIST_CONFIRM)
    if entry_id is None:
        abort(404)
    entry = db.session.get(WaitlistEntry, entry_id) or abort(404)
    instance = db.session.get(ClassInstance, entry.class_instance_id)
    already_expired = entry.status == "released"
    waitlist.confirm(entry)
    db.session.commit()
    return render_template(
        "portal/waitlist_confirmed.html",
        entry=entry,
        instance=instance,
        expired=already_expired,
    )


# ------------------------------------------------------------- my classes ---
@bp.get("/classes")
@login_required
def my_classes():
    ids = [a.id for a in _my_attendees()] or [0]
    rows = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(Booking.attendee_id.in_(ids))
        .order_by(ClassInstance.starts_at_utc.desc())
        .limit(60)
        .all()
    )
    upcoming = [
        b
        for b in rows
        if b.status == BookingStatus.booked.value
        and b.class_instance.starts_at_utc > now_utc()
    ]
    history = [b for b in rows if b not in upcoming]
    return render_template(
        "portal/my_classes.html",
        upcoming=sorted(upcoming, key=lambda b: b.class_instance.starts_at_utc),
        history=history,
        stats={a.id: _streak_stats(a.id) for a in _my_attendees()},
        attendees=_my_attendees(),
    )


def _streak_stats(attendee_id: int) -> dict:
    attended = (
        db.session.query(Booking)
        .join(ClassInstance)
        .filter(
            Booking.attendee_id == attendee_id,
            Booking.status == BookingStatus.attended.value,
        )
        .all()
    )
    weeks = {b.class_instance.local_date.isocalendar()[:2] for b in attended}
    streak = 0
    probe = today_local()
    while probe.isocalendar()[:2] in weeks:
        streak += 1
        probe -= timedelta(weeks=1)
    month_visits = sum(
        1
        for b in attended
        if b.class_instance.local_date.month == today_local().month
        and b.class_instance.local_date.year == today_local().year
    )
    return {"streak_weeks": streak, "month_visits": month_visits}


# -------------------------------------------------------------- membership ---
CANCEL_REASONS = [
    ("pause_would_have_saved", "I'd have stayed if I could pause for a while"),
    ("schedule", "The schedule stopped working for us"),
    ("cost", "Cost"),
    ("moving", "Moving away"),
    ("injury_health", "Injury or health"),
    ("lost_interest", "Lost interest"),
    ("other", "Other"),
]


@bp.get("/membership")
@login_required
def membership():
    subs = (
        db.session.query(Subscription)
        .filter_by(user_id=current_user.id)
        .order_by(Subscription.id.desc())
        .all()
    )
    payments = (
        db.session.query(Payment)
        .filter_by(user_id=current_user.id)
        .order_by(Payment.created_at.desc())
        .limit(24)
        .all()
    )
    plans = {p.id: p for p in db.session.query(Plan).all()}
    attendees = {a.id: a for a in current_user.attendees}
    customer = (
        db.session.query(StripeCustomer)
        .filter_by(user_id=current_user.id)
        .one_or_none()
    )
    return render_template(
        "portal/membership.html",
        subs=subs,
        payments=payments,
        plans=plans,
        attendees=attendees,
        customer=customer,
        card_token=make_token(current_user.id, SALT_UPDATE_CARD),
    )


@bp.route("/membership/<int:sub_id>/cancel", methods=["GET", "POST"])
@login_required
def cancel_membership(sub_id: int):
    from ..services import billing

    sub = db.session.get(Subscription, sub_id) or abort(404)
    if sub.user_id != current_user.id:
        abort(404)
    attendee = db.session.get(AttendeeProfile, sub.attendee_id)
    if request.method == "POST":
        reason = request.form.get("reason")
        if reason not in {r[0] for r in CANCEL_REASONS}:
            flash("Pick a reason so we can keep improving.", "error")
        else:
            billing.cancel_subscription(
                sub, reason=reason, note=(request.form.get("note") or "").strip() or None
            )
            db.session.commit()
            if sub.status == "cancelled":
                flash(
                    f"{attendee.first_name}'s membership is cancelled before any "
                    "charge — nothing owed. The ring's here whenever you're ready.",
                    "success",
                )
            else:
                from ..services.tzutil import fmt_local

                flash(
                    "Cancellation notice received (in writing, per the membership "
                    f"terms). {attendee.first_name}'s membership and billing "
                    f"continue through the 30-day notice period and end on "
                    f"{fmt_local(sub.cancel_effective_at, '%B %d, %Y')}. Classes "
                    "remain bookable until then.",
                    "success",
                )
            return redirect(url_for("portal.membership"))
    return render_template(
        "portal/cancel_membership.html",
        sub=sub,
        attendee=attendee,
        reasons=CANCEL_REASONS,
    )


# ------------------------------------------------------- waivers, settings ---
@bp.route("/waivers", methods=["GET", "POST"])
@login_required
def waivers():
    attendees = _my_attendees()
    latest = {
        d.kind: d
        for d in db.session.query(WaiverDocument)
        .filter_by(client_account_id=current_user.client_account_id, active=True)
        .order_by(WaiverDocument.version)
        .all()
    }
    status = []
    for a in attendees:
        kind = "minor" if a.kind == "child" else "adult"
        doc = latest.get(kind)
        sig = (
            db.session.query(WaiverSignature)
            .filter_by(attendee_id=a.id)
            .order_by(WaiverSignature.signed_at.desc())
            .first()
        )
        needs_resign = bool(doc) and (sig is None or sig.document_id != doc.id)
        status.append({"attendee": a, "doc": doc, "sig": sig, "needs_resign": needs_resign})

    if request.method == "POST":
        attendee = _own_attendee(request.form.get("attendee_id", type=int) or 0)
        signature = (request.form.get("signature") or "").strip()
        if len(signature) < 2:
            flash("Type your full legal name to sign.", "error")
        else:
            sign_waiver(attendee, current_user, signature)
            db.session.commit()
            flash(f"Waiver re-signed for {attendee.first_name}.", "success")
            return redirect(url_for("portal.waivers"))
    return render_template("portal/waivers.html", status=status)


DEFAULT_PREFS = {"reminders": True, "schedule": True, "news": True}


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_user.notify_prefs = {
            k: request.form.get(f"pref_{k}") == "on" for k in DEFAULT_PREFS
        }
        # CASL marketing consent — separate from transactional categories
        was_consented = current_user.consent_email or current_user.consent_sms
        current_user.consent_email = request.form.get("consent_email") == "on"
        current_user.consent_sms = request.form.get("consent_sms") == "on"
        if (current_user.consent_email or current_user.consent_sms) and not was_consented:
            current_user.consent_captured_at = utcnow()
        db.session.commit()
        flash("Preferences saved.", "success")
        return redirect(url_for("portal.settings"))
    prefs = {**DEFAULT_PREFS, **(current_user.notify_prefs or {})}
    return render_template("portal/settings.html", prefs=prefs)


@bp.get("/referrals")
@login_required
def referrals():
    code = _ensure_referral_code(current_user)
    from ..models import Lead

    referred = (
        db.session.query(Lead)
        .filter_by(referral_code=code)
        .order_by(Lead.created_at.desc())
        .all()
    )
    share_url = absolute_url("funnel.landing_kids") + f"?ref={code}"
    return render_template(
        "portal/referrals.html", code=code, referred=referred, share_url=share_url
    )


# ------------------------------------------- signed-link card update page ---
@bp.get("/card/<token>")
def update_card(token: str):
    """Self-serve card update via Stripe Elements (signed link — works even
    for guardians who never set a password). Recovery is webhook-driven."""
    user_id = read_token(token, SALT_UPDATE_CARD)
    if user_id is None:
        abort(404)
    guardian = db.session.get(User, user_id) or abort(404)

    customer, client_secret = stripe_service.ensure_customer_with_setup_intent(
        guardian
    )
    db.session.commit()

    past_due = (
        db.session.query(Subscription)
        .filter_by(user_id=guardian.id, status=SubscriptionStatus.past_due.value)
        .count()
        > 0
    )
    return render_template(
        "portal/update_card.html",
        guardian=guardian,
        past_due=past_due,
        stripe_publishable_key=current_app.config["STRIPE_PUBLISHABLE_KEY"],
        client_secret=client_secret,
        stripe_configured=stripe_service.is_configured(),
    )
