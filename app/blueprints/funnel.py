"""Marketing funnel. Pass 1 ships the Youth journey — the guardian/child path
is the PRIMARY journey, not an edge case: the parent is the customer, the
child is the attendee.

/kids (custom) + /<slug>    landing pages (youth/technical/bootcamp/shehits/beast)
/book/<segment>             step 1: pick a class (live capacity, age brackets)
/book/<segment>/details     step 2: who's attending + health + consents + waiver
/book/<segment>/card        step 3: SetupIntent card vault (no charge today)
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
# Booking-flow segments = class segment_tags (client schedule 2026-08-20):
# kids 6-10 (4pm groups), youth 11-18 (7pm confidence), technical (6pm),
# bootcamp (5pm), shehits (10am), beast (6am). Retired landings /reset,
# /focus, /strong 301 to their nearest successor.
SEGMENTS = {"kids", "youth", "technical", "bootcamp", "shehits", "beast"}
CHILD_FIRST_SEGMENTS = {"kids", "youth"}  # a parent books; the child attends

# Master Plan §6 wording, adjusted ONLY for the confirmed billing cadence:
# $189 per 4-week cycle (client 2026-07-24), so "/month" would be inaccurate
# (13 charges/year) and disclosure accuracy is a card-network requirement.
DISCLOSURE = (
    "No charge today. Your membership is {price} + 5% GST ({total} billed "
    "every 4 weeks), starting after your free class on {date}, unless you "
    "cancel. We'll remind you before any charge. Cancel before the first "
    "charge in one click."
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


@bp.get("/sitemap.xml")
def sitemap():
    base = current_app.config["SITE_BASE_URL"].rstrip("/")
    paths = [
        "/", "/kids", "/youth", "/technical", "/bootcamp", "/shehits",
        "/beast", "/schedule", "/pricing", "/trainers", "/contact",
        "/privacy", "/terms",
    ]
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in paths)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return body, 200, {"Content-Type": "application/xml"}


@bp.get("/robots.txt")
def robots():
    body = (
        "User-agent: *\n"
        "Disallow: /ops/\n"
        "Disallow: /portal/\n"
        "Disallow: /api/\n"
        "Disallow: /book/\n"
        "Allow: /\n"
        f"Sitemap: {current_app.config['SITE_BASE_URL'].rstrip('/')}/sitemap.xml\n"
    )
    return body, 200, {"Content-Type": "text/plain"}


@bp.post("/webhooks/stripe")
def stripe_webhook_root():
    """Top-level webhook path per the deploy brief; same handler as
    /api/v1/webhooks/stripe. CSRF-exempted in create_app (Stripe signs its
    own requests)."""
    from .api import stripe_webhook

    return stripe_webhook()


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


@bp.get("/kids")
def landing_kids():
    """Custom kids page (ages 6-10) — was /youth until the 2026-08-20
    program split; /youth is now the 11-18 confidence class landing."""
    client = get_client()
    reviews = (
        db.session.query(Review)
        .filter(Review.client_account_id == client.id, Review.active.is_(True))
        .order_by(Review.display_order, Review.id)
        .all()
    )
    reviews = [r for r in reviews if r.tags() & {"kids", "youth", "parents"}][:4]
    variant = request.args.get("v", "a")[:20]
    resp = make_response(
        render_template(
            "funnel/landing_kids.html",
            google_rating=SiteSetting.get("google_rating", "5.0"),
            google_review_count=SiteSetting.get("google_review_count", "28"),
            reviews=reviews,
        )
    )
    capture_first_touch(request, resp, landing_variant=f"kids:{variant}")
    return resp


# Retired landings — ads or bookmarks may still point here.
@bp.get("/reset")
def landing_reset_retired():
    return redirect(url_for("funnel.landing_kids"), 301)


@bp.get("/focus")
def landing_focus_retired():
    return redirect(url_for("funnel.landing", slug="bootcamp"), 301)


@bp.get("/strong")
def landing_strong_retired():
    return redirect(url_for("funnel.landing", slug="technical"), 301)


@bp.get("/<slug>")
def landing(slug: str):
    """The copy-config landing pages (/kids keeps its custom page)."""
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
            # Hard guard: a blank child name slipped past form validation
            # once in production (2026-08-31) — never store one.
            if not (form.child_first_name.data or "").strip():
                from ..legal import WAIVER_SECTIONS

                flash("Please enter your child's first name.", "error")
                return render_template(
                    "funnel/step_details.html", form=form, instance=instance,
                    health_questions=HEALTH_QUESTIONS,
                    waiver_sections=WAIVER_SECTIONS, segment=segment, step=2,
                )
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
            submit_ip=request.remote_addr,
            submit_user_agent=(request.user_agent.string or "")[:255],
        )
        db.session.add(lead)
        db.session.flush()

        booking, created = booking_flow.create_trial_booking(
            instance,
            attendee,
            lead,
            walkin=request.args.get("source") == "walkin",
        )
        if created:
            booking_flow.send_booking_confirmation(booking)
            booking_flow.send_admin_signup_alert(booking)
        else:
            # Re-submitted form: keep the booking's original lead so ad
            # counts don't inflate, and drop the one minted above.
            db.session.delete(lead)
        db.session.commit()

        state.update(
            {
                "booking_id": booking.id,
                "lead_id": booking.lead_id,
                "guardian_id": guardian.id,
            }
        )
        _save(state)
        return redirect(url_for("funnel.step_card", segment=segment))

    from ..legal import WAIVER_SECTIONS

    return render_template(
        "funnel/step_details.html",
        form=form,
        instance=instance,
        health_questions=HEALTH_QUESTIONS,
        waiver_sections=WAIVER_SECTIONS,
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
    from ..models import Subscription, SubscriptionStatus
    from ..services.billing import family_price_cents
    from ..services.tax import fmt_cents, total_with_gst_cents

    cents = plan_row.price_cents if plan_row else 18900
    family_note = ""
    if plan_row:
        # Family pricing: quote the ACTUAL tier this member would pay,
        # based on the guardian's existing live memberships.
        live = (
            db.session.query(Subscription)
            .filter(
                Subscription.user_id == booking.attendee.guardian.id,
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
        tier = family_price_cents(live, plan_row)
        if tier < cents:
            cents = tier
            family_note = " Family pricing applied for your additional member."
    class_date_str = fmt_local(booking.class_instance.starts_at_utc, "%A, %B %d")
    disclosure = (
        DISCLOSURE.format(
            price=fmt_cents(cents),
            total=fmt_cents(total_with_gst_cents(cents)),
            date=class_date_str,
        )
        + family_note
    )

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

    # Browser-side conversion events, sharing the outbox event_id so Meta
    # dedups them against the CAPI mirror. Ads optimize on these.
    pixel_events = []
    if current_app.config["META_PIXEL_ID"] and booking.lead_id:
        from ..models import EventOutbox

        rows = (
            db.session.query(EventOutbox)
            .filter(
                EventOutbox.client_account_id == booking.client_account_id,
                EventOutbox.event_name.in_(("Lead", "Schedule")),
            )
            .order_by(EventOutbox.id.desc())
            .limit(50)
            .all()
        )
        seen: set[str] = set()
        for r in rows:
            if (r.payload or {}).get("lead_id") == booking.lead_id and (
                r.event_name not in seen
            ):
                pixel_events.append({"name": r.event_name, "id": r.event_id})
                seen.add(r.event_name)
    return render_template(
        "funnel/confirmed.html", booking=booking, pixel_events=pixel_events
    )


@bp.route("/activate/<token>", methods=["GET", "POST"])
def activate_membership(token: str):
    """Member self-confirm after the free class (signed link in the post-class
    email). GET shows the confirm page; POST activates the subscription."""
    from ..services import billing
    from ..services.signed_links import SALT_ACTIVATE

    from ..models import PaymentMethodStatus, StripeCustomer
    from ..services.signed_links import SALT_UPDATE_CARD, make_token
    from ..services.tax import price_with_gst_label

    booking_id = read_token(token, SALT_ACTIVATE)
    if booking_id is None:
        abort(404)
    booking = db.session.get(Booking, booking_id) or abort(404)
    attendee = booking.attendee
    plan = billing.default_plan(booking.client_account_id)

    # Cardless members (skipped the card step at booking) need to vault a
    # card first — route them to the card page instead of a dead-end error.
    guardian = attendee.guardian
    sc = (
        db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    )
    has_card = bool(sc and sc.payment_method_status == PaymentMethodStatus.vaulted.value)
    card_url = url_for(
        "portal.update_card", token=make_token(guardian.id, SALT_UPDATE_CARD)
    )
    price_label = None
    if plan:
        from ..models import Subscription, SubscriptionStatus
        from ..services.billing import family_price_cents

        live = (
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
        price_label = price_with_gst_label(family_price_cents(live, plan))

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
        has_card=has_card,
        card_url=card_url,
        price_label=price_label,
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
        "funnel/membership_cancelled.html", charged_yet=charged_yet, sub=sub
    )


@bp.get("/calendar/<token>.ics")
def booking_ics(token: str):
    """Signed .ics download for the add-to-calendar button."""
    from flask import Response

    from ..services.calendar_links import SALT_CALENDAR, ics_content

    booking_id = read_token(token, SALT_CALENDAR)
    if booking_id is None:
        abort(404)
    booking = db.session.get(Booking, booking_id) or abort(404)
    return Response(
        ics_content(booking),
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=box2fit-class.ics"},
    )


@bp.get("/confirm/<token>")
def confirm_attendance(token: str):
    """One-click 'I'm coming' from the reminder email. Marks the booking
    confirmed (staff see it on the roster); real attendance still happens at
    the door."""
    from ..services.signed_links import SALT_CONFIRM_ATTEND

    booking_id = read_token(token, SALT_CONFIRM_ATTEND)
    if booking_id is None:
        abort(404)
    booking = db.session.get(Booking, booking_id) or abort(404)
    already_cancelled = booking.status == BookingStatus.cancelled.value
    if booking.status == BookingStatus.booked.value and booking.confirmed_at is None:
        booking.confirmed_at = utcnow()
        db.session.commit()
    return render_template(
        "funnel/confirm_attendance.html",
        booking=booking,
        already_cancelled=already_cancelled,
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
