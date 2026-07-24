"""Marketing funnel. Pass 1 ships the Youth journey — the guardian/child path
is the PRIMARY journey, not an edge case: the parent is the customer, the
child is the attendee.

/youth                      landing page
/book/youth                 step 1: pick a class (live capacity, age brackets)
/book/youth/details         step 2: who's attending + health + consents + waiver
/book/youth/card            step 3: SetupIntent card vault (no charge today)
/book/complete              Stripe Elements return
/book/confirmed             confirmation
/book/cancel/<token>        signed one-click cancel
"""
import logging

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..extensions import db, limiter
from ..models import (
    Booking,
    BookingStatus,
    ClassInstance,
    ClientAccount,
    Lead,
    LeadStatus,
    Review,
    SiteSetting,
    StripeCustomer,
    utcnow,
)
from ..services import booking_flow, stripe_service
from ..services.attribution import capture_first_touch, read_first_touch
from ..services.scheduling import (
    upcoming_instances,
    validate_age,
    validate_bookable,
)
from ..services.signed_links import SALT_CANCEL_BOOKING, read_token
from ..services.tracking import enqueue_event
from ..services.tzutil import fmt_local, now_utc
from .forms import HEALTH_QUESTIONS, AttendeeForm, SlotForm

bp = Blueprint("funnel", __name__)
log = logging.getLogger(__name__)

SESSION_KEY = "b2f_flow"
# Booking-flow segments = class segment_tags. "reset" (parents landing) books
# into the youth flow, so it is not its own booking segment.
SEGMENTS = {"youth", "strong", "focus", "shehits", "beast"}
CHILD_FIRST_SEGMENTS = {"youth"}  # child is the default attendee here

# Master Plan §6 wording, adjusted ONLY for the confirmed billing cadence:
# $189 per 4-week cycle (client 2026-07-24), so "/month" would be inaccurate
# (13 charges/year) and disclosure accuracy is a card-network requirement.
DISCLOSURE = (
    "No charge today. Your {price} membership, billed every 4 weeks, starts "
    "after your free class on {date}, unless you cancel. We'll remind you "
    "before any charge. Cancel anytime in one click."
)


def get_client() -> ClientAccount:
    client = (
        db.session.query(ClientAccount).filter_by(active=True).order_by(
            ClientAccount.id
        ).first()
    )
    if client is None:
        abort(503, "No client account configured — run the seed.")
    return client


def _flow(segment: str) -> dict:
    if segment not in SEGMENTS:
        abort(404)
    state = session.get(SESSION_KEY, {})
    if state.get("segment") != segment:
        state = {"segment": segment}
    return state


def _save(state: dict) -> None:
    session[SESSION_KEY] = state


@bp.get("/healthz")
def healthz():
    return {"status": "ok"}


@bp.get("/")
def index():
    """Public homepage (health.box2fit.com) — calm, health-led."""
    client = get_client()
    from ..models import Trainer

    trainers = (
        db.session.query(Trainer)
        .filter_by(client_account_id=client.id, active=True)
        .all()
    )
    reviews = (
        db.session.query(Review)
        .filter(Review.client_account_id == client.id, Review.active.is_(True))
        .order_by(Review.display_order)
        .limit(3)
        .all()
    )
    occurrences = upcoming_instances(client.id, days=7)[:8]
    return render_template(
        "site/home.html",
        google_rating=SiteSetting.get("google_rating", "5.0"),
        google_review_count=SiteSetting.get("google_review_count", "28"),
        trainers=trainers,
        reviews=reviews,
        occurrences=occurrences,
    )


@bp.get("/schedule")
def public_schedule():
    """Public read-only week view — booking happens from your account."""
    client = get_client()
    occurrences = upcoming_instances(client.id, days=7)
    by_day: dict = {}
    for o in occurrences:
        by_day.setdefault(o["instance"].local_date, []).append(o)
    return render_template("site/schedule.html", by_day=by_day)


@bp.get("/trainers")
def public_trainers():
    from ..models import Trainer

    client = get_client()
    trainers = (
        db.session.query(Trainer)
        .filter_by(client_account_id=client.id, active=True)
        .all()
    )
    return render_template("site/trainers.html", trainers=trainers)


@bp.get("/pricing")
def public_pricing():
    from ..models import Plan

    client = get_client()
    plan = (
        db.session.query(Plan)
        .filter_by(client_account_id=client.id, active=True)
        .first()
    )
    return render_template("site/pricing.html", plan=plan)


@bp.get("/contact")
def public_contact():
    return render_template("site/contact.html")


@bp.get("/privacy")
def public_privacy():
    return render_template("site/privacy.html")


@bp.get("/terms")
def public_terms():
    return render_template("site/terms.html")


@bp.get("/youth")
def landing_youth():
    client = get_client()
    reviews = (
        db.session.query(Review)
        .filter(Review.client_account_id == client.id, Review.active.is_(True))
        .order_by(Review.display_order, Review.id)
        .all()
    )
    reviews = [r for r in reviews if "youth" in r.tags() or "parents" in r.tags()][:4]
    variant = request.args.get("v", "a")[:20]
    resp = make_response(
        render_template(
            "funnel/landing_youth.html",
            google_rating=SiteSetting.get("google_rating", "5.0"),
            google_review_count=SiteSetting.get("google_review_count", "28"),
            reviews=reviews,
        )
    )
    capture_first_touch(request, resp, landing_variant=f"youth:{variant}")
    return resp


@bp.get("/<slug>")
def landing(slug: str):
    """The five copy-config landing pages (/youth keeps its custom page)."""
    from ..services.copy_loader import load_copy

    copy = load_copy(slug)
    if copy is None:
        abort(404)
    client = get_client()
    reviews = (
        db.session.query(Review)
        .filter(Review.client_account_id == client.id, Review.active.is_(True))
        .order_by(Review.display_order, Review.id)
        .all()
    )
    wanted = set(copy.get("review_tags") or [slug])
    reviews = [r for r in reviews if wanted & r.tags()][:4]
    variant = request.args.get("v", "a")[:20]
    resp = make_response(
        render_template(
            "funnel/landing.html",
            copy=copy,
            google_rating=SiteSetting.get("google_rating", "5.0"),
            google_review_count=SiteSetting.get("google_review_count", "28"),
            reviews=reviews,
        )
    )
    capture_first_touch(request, resp, landing_variant=f"{slug}:{variant}")
    return resp


@bp.route("/book/<segment>", methods=["GET", "POST"])
@limiter.limit("30/hour", methods=["POST"])
def step_class(segment: str):
    state = _flow(segment)
    client = get_client()
    form = SlotForm()

    if form.validate_on_submit():
        instance = db.session.get(ClassInstance, form.instance_id.data)
        err = validate_bookable(instance, for_trial=True)
        if err:
            flash(err, "error")
        else:
            state["instance_id"] = instance.id
            _save(state)
            return redirect(url_for("funnel.step_details", segment=segment))

    occurrences = upcoming_instances(
        client.id, segment_tag=segment, trials_only=True
    )
    return render_template(
        "funnel/step_class.html",
        form=form,
        occurrences=occurrences,
        segment=segment,
        step=1,
    )


@bp.route("/book/<segment>/details", methods=["GET", "POST"])
@limiter.limit("10/hour", methods=["POST"])
def step_details(segment: str):
    state = _flow(segment)
    if "instance_id" not in state:
        return redirect(url_for("funnel.step_class", segment=segment))
    client = get_client()
    instance = db.session.get(ClassInstance, state["instance_id"])
    form = AttendeeForm()
    if request.method == "GET" and segment not in CHILD_FIRST_SEGMENTS:
        form.attendee_kind.data = "self"  # adults book themselves by default

    if form.validate_on_submit():
        err = validate_bookable(instance, for_trial=True)
        if err:
            flash(err, "error")
            return redirect(url_for("funnel.step_class", segment=segment))

        guardian = booking_flow.get_or_create_guardian(
            client.id,
            name=form.guardian_name.data.strip(),
            email=form.email.data.strip().lower(),
            phone=form.normalized_phone.data,
            consent_email=form.consent_email.data,
            consent_sms=form.consent_sms.data,
        )
        if form.attendee_kind.data == "child":
            attendee = booking_flow.create_child_attendee(
                guardian,
                first_name=form.child_first_name.data.strip(),
                birth_year=form.child_birth_year.data,
                emergency_contact_name=form.emergency_contact_name.data.strip(),
                emergency_contact_phone=form.normalized_ec_phone.data
                or form.emergency_contact_phone.data,
                health_answers=form.health_answers(),
            )
        else:
            attendee = booking_flow.create_self_attendee(
                guardian, form.health_answers()
            )

        age_err = validate_age(instance.class_type, attendee)
        if age_err:
            db.session.rollback()
            flash(age_err, "error")
            return redirect(url_for("funnel.step_class", segment=segment))

        booking_flow.sign_waiver(attendee, guardian, form.signature.data.strip())

        touch = read_first_touch(request)
        lead = Lead(
            client_account_id=client.id,
            user_id=guardian.id,
            name=guardian.name,
            email=guardian.email,
            phone=guardian.phone,
            segment=segment,
            status=LeadStatus.new.value,
            utm_source=touch.get("utm_source"),
            utm_medium=touch.get("utm_medium"),
            utm_campaign=touch.get("utm_campaign"),
            utm_content=touch.get("utm_content"),
            utm_term=touch.get("utm_term"),
            landing_variant=touch.get("landing_variant") or segment,
            referral_code=touch.get("referral_code"),
            first_touch_at=utcnow(),
        )
        db.session.add(lead)
        db.session.flush()

        booking = booking_flow.create_trial_booking(
            instance,
            attendee,
            lead,
            walkin=request.args.get("source") == "walkin",
        )
        booking_flow.send_booking_confirmation(booking)
        db.session.commit()

        state.update(
            {"booking_id": booking.id, "lead_id": lead.id, "guardian_id": guardian.id}
        )
        _save(state)
        return redirect(url_for("funnel.step_card", segment=segment))

    return render_template(
        "funnel/step_details.html",
        form=form,
        instance=instance,
        health_questions=HEALTH_QUESTIONS,
        segment=segment,
        step=2,
    )


@bp.get("/book/<segment>/card")
def step_card(segment: str):
    state = _flow(segment)
    if "booking_id" not in state:
        return redirect(url_for("funnel.step_class", segment=segment))
    booking = db.session.get(Booking, state["booking_id"]) or abort(404)
    attendee = booking.attendee
    guardian = attendee.guardian
    lead = db.session.get(Lead, state.get("lead_id"))

    customer, client_secret = stripe_service.ensure_customer_with_setup_intent(
        guardian, lead
    )
    db.session.commit()

    class_type = booking.class_instance.class_type
    from ..models import Plan

    plan_row = (
        db.session.query(Plan)
        .filter_by(client_account_id=booking.client_account_id, active=True)
        .filter(
            (Plan.class_type_id == class_type.id) | (Plan.class_type_id.is_(None))
        )
        .order_by(Plan.class_type_id.desc())
        .first()
    )
    cents = plan_row.price_cents if plan_row else 18900
    price_str = f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"
    class_date_str = fmt_local(booking.class_instance.starts_at_utc, "%A, %B %d")
    disclosure = DISCLOSURE.format(price=price_str, date=class_date_str)

    return render_template(
        "funnel/step_card.html",
        booking=booking,
        attendee=attendee,
        disclosure=disclosure,
        stripe_publishable_key=current_app.config["STRIPE_PUBLISHABLE_KEY"],
        client_secret=client_secret,
        stripe_configured=stripe_service.is_configured(),
        return_url=url_for("funnel.complete", _external=True),
        segment=segment,
        step=3,
    )


@bp.get("/book/complete")
def complete():
    state = session.get(SESSION_KEY, {})
    booking_id = state.get("booking_id")
    if not booking_id:
        return redirect(url_for("funnel.index"))
    booking = db.session.get(Booking, booking_id) or abort(404)
    guardian = booking.attendee.guardian
    customer = (
        db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    )
    if customer and stripe_service.confirm_setup_intent_vaulted(customer):
        lead = db.session.get(Lead, state.get("lead_id"))
        enqueue_event(
            "AddPaymentInfo", booking.client_account_id, lead, booking_id=booking.id
        )
    db.session.commit()
    return redirect(url_for("funnel.confirmed"))


@bp.post("/book/skip-card")
def skip_card():
    """Dev-only: finishes the flow locally when Stripe keys aren't set."""
    if stripe_service.is_configured():
        abort(404)
    if not session.get(SESSION_KEY, {}).get("booking_id"):
        return redirect(url_for("funnel.index"))
    return redirect(url_for("funnel.confirmed"))


@bp.get("/book/confirmed")
def confirmed():
    state = session.get(SESSION_KEY, {})
    booking_id = state.get("booking_id")
    if not booking_id:
        return redirect(url_for("funnel.index"))
    booking = db.session.get(Booking, booking_id) or abort(404)
    session.pop(SESSION_KEY, None)
    return render_template("funnel/confirmed.html", booking=booking)


@bp.route("/activate/<token>", methods=["GET", "POST"])
def activate_membership(token: str):
    """Member self-confirm after the free class (signed link in the post-class
    email). GET shows the confirm page; POST activates the subscription."""
    from ..services import billing
    from ..services.signed_links import SALT_ACTIVATE

    booking_id = read_token(token, SALT_ACTIVATE)
    if booking_id is None:
        abort(404)
    booking = db.session.get(Booking, booking_id) or abort(404)
    attendee = booking.attendee
    plan = billing.default_plan(booking.client_account_id)

    if request.method == "POST":
        try:
            billing.activate_subscription(
                attendee,
                plan=plan,
                cohort_label=booking.class_instance.cohort_label,
                actor="member",
            )
            db.session.commit()
            return render_template(
                "funnel/activated.html", attendee=attendee, booking=booking
            )
        except billing.ActivationError as exc:
            db.session.rollback()
            flash(str(exc), "error")

    return render_template(
        "funnel/activate.html",
        attendee=attendee,
        booking=booking,
        plan=plan,
        token=token,
    )


@bp.get("/cancel-membership/<token>")
def cancel_membership(token: str):
    """One-click cancel — before the first charge or any time after (no
    contracts). Signed link, no login."""
    from ..models import Subscription
    from ..services import billing
    from ..services.signed_links import SALT_CANCEL_BEFORE_CHARGE

    sub_id = read_token(token, SALT_CANCEL_BEFORE_CHARGE)
    if sub_id is None:
        abort(404)
    sub = db.session.get(Subscription, sub_id) or abort(404)
    charged_yet = sub.activated_at is not None
    billing.cancel_subscription(
        sub, reason="cancelled_before_charge" if not charged_yet else "one_click_link"
    )
    db.session.commit()
    return render_template(
        "funnel/membership_cancelled.html", charged_yet=charged_yet
    )


@bp.get("/book/cancel/<token>")
def cancel_booking(token: str):
    booking_id = read_token(token, SALT_CANCEL_BOOKING)
    if booking_id is None:
        abort(404)
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.status == BookingStatus.booked.value:
        booking.status = BookingStatus.cancelled.value
        booking.cancelled_at = utcnow()
        # Silent late-cancel tracking (staff reporting only, RSVP policy)
        late_hours = current_app.config["POLICY_LATE_CANCEL_HOURS"]
        seconds_left = (
            booking.class_instance.starts_at_utc - now_utc()
        ).total_seconds()
        booking.late_cancel = 0 < seconds_left < late_hours * 3600
        lead = (
            db.session.get(Lead, booking.lead_id) if booking.lead_id else None
        )
        if lead and lead.status == LeadStatus.booked.value:
            lead.status = LeadStatus.cancelled.value
        # a spot just opened — promote the waitlist
        from ..services import waitlist

        waitlist.promote_next(booking.class_instance)
        db.session.commit()
    return render_template("funnel/cancelled.html", booking=booking)
