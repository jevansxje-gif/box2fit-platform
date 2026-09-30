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
from ..services.urls import absolute_url
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
            variant=request.args.get("v", "a")[:20],
            member_price=_member_price_label(client.id),
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


@bp.get("/invite/<token>")
def ad_invite(token: str):
    """A gym-referred ad enquiry: someone who saw the ad but contacted the
    gym directly instead of clicking. Staff send this signed link; it seeds
    the ad attribution server-side (tagged medium=referral, content=
    gym-referral so it's transparent it wasn't a pixel click), then drops
    them into the normal booking flow where the real lead is created."""
    import json

    from ..services.signed_links import SALT_AD_INVITE, read_payload_token

    data = read_payload_token(token, SALT_AD_INVITE)
    if not data:
        abort(404)
    segment = data.get("segment") if data.get("segment") in SEGMENTS else "kids"
    resp = make_response(redirect(url_for("funnel.landing", slug=segment)))
    # Seed first-touch directly (don't rely on query UTMs surviving). First
    # touch still wins for 30 days, so a later real ad click won't clobber
    # a click this person made earlier — matching normal attribution rules.
    if not request.cookies.get(current_app.config["UTM_COOKIE_NAME"]):
        resp.set_cookie(
            current_app.config["UTM_COOKIE_NAME"],
            json.dumps(
                {
                    "utm_source": "meta",
                    "utm_medium": "referral",
                    "utm_campaign": segment,
                    "utm_content": "gym-referral",
                    "landing_variant": f"{segment}:invite",
                    "first_touch_at": utcnow().isoformat(),
                }
            ),
            max_age=current_app.config["UTM_COOKIE_MAX_AGE"],
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
    return resp


@bp.route("/membership/start/<token>", methods=["GET", "POST"])
def membership_pay(token: str):
    """Booking-less membership payment setup: for a phone/other-app member
    who is billed through us for tracking + commission, but whose classes
    live in the gym's own app. Staff send this signed link; the member adds
    a card and a recurring membership starts on our Stripe."""
    from datetime import date as _date

    from ..models import (
        AttendeeProfile,
        PaymentMethodStatus,
        Plan,
        Subscription,
        SubscriptionStatus,
    )
    from ..services import billing
    from ..services.signed_links import SALT_MEMBERSHIP_SETUP, read_payload_token
    from ..services.tax import price_with_gst_label

    data = read_payload_token(token, SALT_MEMBERSHIP_SETUP)
    if not data:
        abort(404)
    attendee = db.session.get(AttendeeProfile, data.get("aid"))
    if attendee is None:
        abort(404)
    guardian = attendee.guardian
    plan = db.session.get(Plan, data.get("plan")) or billing.default_plan(
        attendee.client_account_id
    )
    charge_on = (
        _date.fromisoformat(data["charge_on"]) if data.get("charge_on") else None
    )

    live = (
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
        .count()
    )
    if live:
        return render_template(
            "funnel/membership_pay.html", done=True, guardian=guardian
        )

    from ..models import StripeCustomer

    customer = (
        db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one_or_none()
    )
    vaulted = (
        customer is not None
        and customer.payment_method_status == PaymentMethodStatus.vaulted.value
    )

    # Returning from Stripe after a successful card save, or a card already on
    # file → confirm and start the recurring membership (no new SetupIntent).
    if request.args.get("redirect_status") == "succeeded" or vaulted:
        if customer and not vaulted and stripe_service.is_configured():
            stripe_service.confirm_setup_intent_vaulted(customer)
            db.session.commit()
            vaulted = (
                customer.payment_method_status == PaymentMethodStatus.vaulted.value
            )
        if vaulted:
            try:
                billing.activate_subscription(
                    attendee, plan=plan, actor="payment_link",
                    first_charge_on=charge_on,
                )
                db.session.commit()
                return render_template(
                    "funnel/membership_pay.html", done=True, guardian=guardian
                )
            except billing.ActivationError as exc:
                flash(str(exc), "error")

    # Need a card: mint the SetupIntent and render the Stripe form.
    customer, client_secret = stripe_service.ensure_customer_with_setup_intent(
        guardian
    )
    db.session.commit()
    return render_template(
        "funnel/membership_pay.html",
        done=False,
        guardian=guardian,
        price_label=price_with_gst_label(plan.price_cents) if plan else "",
        charge_on=charge_on,
        stripe_publishable_key=current_app.config["STRIPE_PUBLISHABLE_KEY"],
        client_secret=client_secret,
        stripe_configured=stripe_service.is_configured(),
    )


TIME_SLOTS = [
    ("early", "Early morning (before 7)"),
    ("morning", "Morning (7 to 9)"),
    ("midmorning", "Mid-morning (9 to 11)"),
    ("lunch", "Lunchtime (11 to 1)"),
    ("afternoon", "Afternoon (1 to 4)"),
    ("evening", "After work (5 to 7)"),
    ("late", "Later evening (7 to 9)"),
    ("weekend", "Weekend morning"),
]


@bp.post("/suggest-time")
@limiter.limit("6/hour")
def suggest_time():
    """"This time doesn't work for me": record when they could train, with
    the ad that brought them, so we can see if the schedule is the blocker."""
    from ..models import TimeSuggestion

    program = (request.form.get("program") or "")[:40]
    if request.form.get("return_to") == "picker" and program:
        back = url_for("funnel.step_class", segment=program)
    else:
        back = (
            url_for("funnel.challenge_landing") if program == "challenge"
            else url_for("funnel.landing", slug=program) if program else "/"
        )
    if request.form.get("website"):  # honeypot
        return redirect(back + "?time=thanks#other-time")
    valid = {k for k, _ in TIME_SLOTS}
    times = [t for t in request.form.getlist("times") if t in valid]
    other = (request.form.get("other") or "").strip()[:200]
    if not times and not other:
        return redirect(back + "?time=pick#other-time")
    touch = read_first_touch(request) or {}
    db.session.add(
        TimeSuggestion(
            client_account_id=get_client().id,
            program=program or "unknown",
            times=",".join(times),
            other=other or None,
            name=(request.form.get("name") or "").strip()[:120] or None,
            contact=(request.form.get("contact") or "").strip()[:160] or None,
            utm_source=touch.get("utm_source"),
            utm_campaign=touch.get("utm_campaign"),
            utm_content=touch.get("utm_content"),
            landing_variant=touch.get("landing_variant"),
            submit_ip=(request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())[:64],
        )
    )
    db.session.commit()
    return redirect(back + "?time=thanks#other-time")


def _member_price_label(client_account_id: int) -> str:
    from ..services.billing import default_plan
    from ..services.tax import fmt_cents

    plan = default_plan(client_account_id)
    return fmt_cents(plan.price_cents) if plan else "$189"


@bp.get("/<slug>")
def landing(slug: str):
    """The copy-config landing pages (/kids keeps its custom page)."""
    from ..services.copy_loader import load_copy

    if slug == "beast":  # Guided Boxing (6 am): its offer is the challenge while it's open
        return redirect(url_for("funnel.challenge_landing"), 302)
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
            slug=slug,
            variant=variant,
            member_price=_member_price_label(client.id),
            time_slots=TIME_SLOTS,
            time_state=request.args.get("time"),
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
        slug=segment,
        time_slots=TIME_SLOTS,
        time_state=request.args.get("time"),
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
            from ..services import guided

            answers = form.health_answers()
            answers["guided"] = guided.parse(request.form)
            attendee = booking_flow.create_self_attendee(guardian, answers)

        age_err = validate_age(instance.class_type, attendee)
        if age_err:
            db.session.rollback()
            flash(age_err, "error")
            return redirect(url_for("funnel.step_class", segment=segment))

        window_err = booking_flow.trial_window_error(attendee, instance)
        if window_err:
            db.session.rollback()
            flash(window_err, "error")
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

    from ..services.guided import GUIDED

    return render_template(
        "funnel/step_details.html",
        form=form,
        instance=instance,
        health_questions=HEALTH_QUESTIONS,
        waiver_sections=WAIVER_SECTIONS,
        segment=segment,
        step=2,
        guided=GUIDED if segment not in CHILD_FIRST_SEGMENTS else None,
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
                first_charge_on=booking.first_charge_on,
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
    from ..models import utcnow as _now

    in_challenge = bool(
        sub.challenge_key and sub.first_charge_at and _now() < sub.first_charge_at
    )
    billing.cancel_subscription(
        sub, reason="cancelled_before_charge" if not charged_yet else "one_click_link"
    )
    db.session.commit()
    return render_template(
        "funnel/membership_cancelled.html", charged_yet=charged_yet, sub=sub,
        in_challenge=in_challenge,
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


# ------------------------------------------------ one-off charge payment page ---
@bp.route("/pay/<token>", methods=["GET", "POST"])
def one_off_pay(token: str):
    """Signed link from staff: pay a fixed amount (e.g. a 10-class punch card)
    on Stripe Checkout. Shows the breakdown, one button, no account needed."""
    from flask import abort, redirect, render_template, request

    from ..models import OneOffCharge, User
    from ..services import billing
    from ..services.signed_links import SALT_ONE_OFF, read_token
    from ..services.tax import fmt_cents

    cid = read_token(token, SALT_ONE_OFF, max_age=60 * 60 * 24 * 60)
    charge = db.session.get(OneOffCharge, cid) if cid else None
    if charge is None or charge.status == "cancelled":
        abort(404)
    guardian = db.session.get(User, charge.user_id)
    if charge.status == "paid":
        return render_template("funnel/one_off_done.html", charge=charge, guardian=guardian, fmt=fmt_cents, paid=True)
    error = None
    if request.method == "POST":
        url = billing.start_one_off_checkout(charge, guardian)
        db.session.commit()
        if url:
            return redirect(url)
        error = "Online payment isn't available right now. Please call the gym at (778) 384-6284."
    return render_template("funnel/one_off_pay.html", charge=charge, guardian=guardian, fmt=fmt_cents, error=error)


@bp.get("/pay/<token>/done")
def one_off_done(token: str):
    from flask import abort, render_template, request

    from ..models import OneOffCharge, User
    from ..services import billing
    from ..services.signed_links import SALT_ONE_OFF, read_token
    from ..services.tax import fmt_cents

    cid = read_token(token, SALT_ONE_OFF, max_age=60 * 60 * 24 * 60)
    charge = db.session.get(OneOffCharge, cid) if cid else None
    if charge is None:
        abort(404)
    guardian = db.session.get(User, charge.user_id)
    paid = billing.confirm_one_off_return(charge, request.args.get("session_id"))
    db.session.commit()
    return render_template("funnel/one_off_done.html", charge=charge, guardian=guardian, fmt=fmt_cents, paid=paid)


# ------------------------------------------- 5-Week Guided Boxing Challenge ---
def _challenge_ctx(client):
    from ..services import challenge as ch
    from ..services.billing import default_plan
    from ..services.tax import fmt_cents, gst_cents, total_with_gst_cents

    plan = default_plan(client.id)
    c = ch.CHALLENGE
    return dict(
        c=c,
        spots_left=ch.spots_left(client.id),
        price=fmt_cents(c["price_cents"]),
        price_gst=fmt_cents(gst_cents(c["price_cents"])),
        price_total=fmt_cents(total_with_gst_cents(c["price_cents"])),
        renew_price=fmt_cents(plan.price_cents) if plan else "",
        renew_total=fmt_cents(total_with_gst_cents(plan.price_cents)) if plan else "",
        waiver_sections=__import__("app.legal", fromlist=["WAIVER_SECTIONS"]).WAIVER_SECTIONS,
    )


@bp.get("/challenge")
def challenge_landing():
    client = get_client()
    resp = make_response(
        render_template(
            "funnel/challenge.html",
            slug="challenge",
            time_slots=TIME_SLOTS,
            time_state=request.args.get("time"),
            **_challenge_ctx(client),
        )
    )
    capture_first_touch(request, resp, landing_variant=f"challenge:{request.args.get('v', 'a')[:20]}")
    return resp


@bp.route("/challenge/join", methods=["GET", "POST"])
@limiter.limit("10/hour", methods=["POST"])
def challenge_join():
    from ..models import Lead, LeadStatus
    from ..services import booking_flow
    from ..services import challenge as ch

    client = get_client()
    ctx = _challenge_ctx(client)
    from ..services import guided
    from ..services.guided import GUIDED

    ctx["guided"] = GUIDED
    form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "phone", "goal", "notes", "signature")}
    error = None
    if request.method == "POST":
        email = form["email"].lower()
        if request.form.get("website"):  # honeypot
            return redirect(url_for("funnel.challenge_landing"))
        if not form["name"] or "@" not in email or "." not in email.split("@")[-1]:
            error = "Please enter your full name and a valid email."
        elif len("".join(ch_ for ch_ in form["phone"] if ch_.isdigit())) < 10:
            error = "Please enter a mobile number so your coach can reach you."
        elif not request.form.get("waiver_agree") or not form["signature"]:
            error = "Please open and read the three sections, tick the box and type your full name to sign."
        elif ctx["spots_left"] <= 0:
            error = "This challenge is full. Tell us which time would suit you below and you'll be first to hear about the next one."
        if error is None:
            guardian = booking_flow.get_or_create_guardian(
                client.id, name=form["name"], email=email, phone=form["phone"],
                consent_email=True, consent_sms=bool(request.form.get("consent_sms")),
            )
            db.session.flush()
            g = guided.parse(request.form)
            attendee = booking_flow.create_self_attendee(
                guardian, {"notes": form["notes"] or (g or {}).get("notes") or "", "challenge_goal": form["goal"] or (g or {}).get("success") or "", "guided": g}
            )
            if ch.existing_sub(attendee):
                db.session.commit()
                return render_template("funnel/challenge_done.html", already=True, guardian=guardian, **ctx)
            booking_flow.sign_waiver(attendee, guardian, form["signature"])
            touch = read_first_touch(request) or {}
            if not db.session.query(Lead).filter_by(user_id=guardian.id).first():
                db.session.add(
                    Lead(
                        client_account_id=client.id, user_id=guardian.id, name=guardian.name,
                        email=guardian.email, phone=guardian.phone or "", segment="guided",
                        status=LeadStatus.new.value,
                        utm_source=touch.get("utm_source"), utm_medium=touch.get("utm_medium"),
                        utm_campaign=touch.get("utm_campaign"), utm_content=touch.get("utm_content"),
                        landing_variant=touch.get("landing_variant"),
                        submit_ip=(request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())[:64],
                    )
                )
            db.session.commit()
            url = ch.start_checkout(guardian, attendee)
            if url:
                return redirect(url)
            # Stripe not configured (dev/test): hold the spot locally.
            ch._local_sub(attendee, None)
            db.session.commit()
            return render_template("funnel/challenge_done.html", already=False, guardian=guardian, paid=False, **ctx)
    return render_template("funnel/challenge_join.html", form=form, error=error, **ctx)


@bp.get("/challenge/done")
def challenge_done():
    """Back from Stripe Checkout: verify the session and record everything
    here as well as on the webhook."""
    import json as _json

    from ..models import User
    from ..services import challenge as ch
    from ..services import stripe_service as _ss

    client = get_client()
    ctx = _challenge_ctx(client)
    sid = request.args.get("session_id")
    guardian, paid = None, False
    if sid and _ss.is_configured():
        try:
            session = _json.loads(str(_ss.stripe_client().checkout.Session.retrieve(sid, stripe_version="2023-10-16")))
            sub = ch.finalize(session)
            db.session.commit()
            if sub is not None:
                guardian = db.session.get(User, sub.user_id)
                paid = sub.activated_at is not None
        except Exception:  # noqa: BLE001
            log.exception("challenge done: could not verify session %s", sid)
    return render_template("funnel/challenge_done.html", already=False, guardian=guardian, paid=paid, **ctx)


# ---------------------------------------------- She Hits: 2 weeks for $94.50 ---
def _shehits_ctx():
    from ..legal import WAIVER_SECTIONS
    from ..services.challenge import SHEHITS_INTRO
    from ..services.guided import GUIDED
    from ..services.tax import fmt_cents, gst_cents, total_with_gst_cents

    o = SHEHITS_INTRO
    return dict(
        price=fmt_cents(o["price_cents"]), price_gst=fmt_cents(gst_cents(o["price_cents"])),
        price_total=fmt_cents(total_with_gst_cents(o["price_cents"])), days=o["days"],
        renew_price=_member_price_label(get_client().id),
        renew_total=fmt_cents(total_with_gst_cents(__import__("app.services.billing", fromlist=["default_plan"]).default_plan(get_client().id).price_cents)),
        waiver_sections=WAIVER_SECTIONS, guided=GUIDED,
    )


@bp.route("/shehits/start", methods=["GET", "POST"])
@limiter.limit("10/hour", methods=["POST"])
def shehits_start():
    """Two weeks of She Hits, $94.50 + GST today; membership continues from
    day 15 unless cancelled (same mechanism as the challenge). Waiver,
    signature and the Guided Start questions here, card on Stripe."""
    from ..models import Lead, LeadStatus
    from ..services import booking_flow, guided
    from ..services import challenge as ch

    client = get_client()
    ctx = _shehits_ctx()
    form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "phone", "signature")}
    error = None
    if request.method == "POST":
        email = form["email"].lower()
        if request.form.get("website"):
            return redirect(url_for("funnel.landing", slug="shehits"))
        if not form["name"] or "@" not in email or "." not in email.split("@")[-1]:
            error = "Please enter your full name and a valid email."
        elif len("".join(c for c in form["phone"] if c.isdigit())) < 10:
            error = "Please enter a mobile number."
        elif not request.form.get("waiver_agree") or not form["signature"]:
            error = "Please open and read the three sections, tick the box and type your full name to sign."
        if error is None:
            guardian = booking_flow.get_or_create_guardian(
                client.id, name=form["name"], email=email, phone=form["phone"],
                consent_email=True, consent_sms=bool(request.form.get("consent_sms")),
            )
            db.session.flush()
            g = guided.parse(request.form)
            attendee = booking_flow.create_self_attendee(guardian, {"notes": (g or {}).get("notes") or "", "guided": g})
            if ch.existing_sub(attendee):
                db.session.commit()
                return render_template("funnel/shehits_done.html", paid=True, already=True, guardian=guardian, **ctx)
            booking_flow.sign_waiver(attendee, guardian, form["signature"])
            touch = read_first_touch(request) or {}
            if not db.session.query(Lead).filter_by(user_id=guardian.id).first():
                db.session.add(Lead(
                    client_account_id=client.id, user_id=guardian.id, name=guardian.name, email=guardian.email,
                    phone=guardian.phone or "", segment="shehits", status=LeadStatus.new.value,
                    utm_source=touch.get("utm_source"), utm_medium=touch.get("utm_medium"),
                    utm_campaign=touch.get("utm_campaign"), utm_content=touch.get("utm_content"),
                    landing_variant=touch.get("landing_variant"),
                ))
            db.session.commit()
            url = ch.start_checkout(guardian, attendee, ch.SHEHITS_INTRO, "funnel.shehits_start_done", "funnel.shehits_start")
            if url:
                return redirect(url)
            ch._local_sub(attendee, None, ch.SHEHITS_INTRO)  # Stripe not configured (dev/test)
            db.session.commit()
            return render_template("funnel/shehits_done.html", paid=True, already=False, guardian=guardian, **ctx)
    return render_template("funnel/shehits_start.html", form=form, error=error, **ctx)


@bp.get("/shehits/start/done")
def shehits_start_done():
    import json as _json

    from ..models import User
    from ..services import challenge as ch
    from ..services import stripe_service as _ss

    ctx = _shehits_ctx()
    sid = request.args.get("session_id")
    guardian, paid = None, False
    if sid and _ss.is_configured():
        try:
            sd = _json.loads(str(_ss.stripe_client().checkout.Session.retrieve(sid, stripe_version="2023-10-16")))
            sub = ch.finalize(sd)
            db.session.commit()
            if sub is not None:
                guardian = db.session.get(User, sub.user_id)
                paid = sub.activated_at is not None
        except Exception:  # noqa: BLE001
            log.exception("she hits done: could not verify session %s", sid)
    return render_template("funnel/shehits_done.html", paid=paid, already=False, guardian=guardian, **ctx)
