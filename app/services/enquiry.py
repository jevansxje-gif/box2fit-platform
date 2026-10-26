"""Call-first short form (owner 2026-10-26).

The landing page stays as it is; its button opens a short form (name,
mobile, date of birth, the class from the page). The gym calls everyone.
When they agree, staff press "Send sign-up link" on the Call list and the
person lands on the normal pay or booking page with their details filled
in. Nothing here takes money: the existing pages still do that.
"""
from datetime import date

from flask import render_template, session

from ..extensions import db
from ..models import (
    Booking,
    Enquiry,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from .booking_flow import admin_alert_recipients
from .messaging import send_email, send_sms
from .signed_links import SALT_ENQUIRY, make_token
from .tzutil import today_local
from .urls import absolute_url

# What each short form is for. `pay` is where the sign-up link (and the
# "Ready now?" link on the thank-you page) sends them.
OFFERS = {
    "challenge": {
        "label": "5-Week Guided Boxing Challenge",
        "class_line": "Monday to Friday at 6 am · coached, small group",
        "pay": ("funnel.challenge_join", {}),
        "back": ("funnel.challenge_landing", {}),
        "kids": False,
        "pay_cta": "Claim my spot and pay",
    },
    "shehits": {
        "label": "She Hits: two weeks for $94.50 + GST",
        "class_line": "Women only · weekdays at 9 am · coached",
        "pay": ("funnel.shehits_start", {}),
        "back": ("funnel.landing", {"slug": "shehits"}),
        "kids": False,
        "pay_cta": "Start my two weeks",
    },
    "kids": {
        "label": "Kids Boxing (ages 6 to 10): free first week",
        "class_line": "Monday, Wednesday and Friday at 4 pm",
        "pay": ("funnel.step_class", {"segment": "kids"}),
        "back": ("funnel.landing_kids", {}),
        "kids": True,
        "pay_cta": "Book the free week now",
    },
}

# Kids ads that get the call-first form instead of the free-week booking
# (test against F5, which keeps booking directly).
KIDS_CALL_FIRST_VARIANTS = {"f2"}


def kids_call_first(v: str | None, utm_content: str | None) -> bool:
    """The kids page shows the short form for the F2 ad (?v=f2 or
    utm_content=families-f2)."""
    codes = {(v or "").lower(), (utm_content or "").rsplit("-", 1)[-1].lower()}
    return bool(codes & KIDS_CALL_FIRST_VARIANTS)

STATUSES = [
    ("new", "New, not called yet"),
    ("no_answer", "No answer"),
    ("call_back", "Call back"),
    ("agreed", "Agreed, link sent"),
    ("not_interested", "Not interested"),
]
OPEN_STATUSES = {"new", "no_answer", "call_back"}

SESSION_KEY = "enquiry_id"


def age_on(dob: date | None, today: date | None = None) -> int | None:
    if dob is None:
        return None
    today = today or today_local()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def parse_dob(raw: str) -> date | None:
    try:
        d = date.fromisoformat((raw or "").strip())
    except ValueError:
        return None
    if d >= today_local() or d.year < 1920:
        return None
    return d


def digits(phone: str) -> str:
    return "".join(c for c in phone or "" if c.isdigit())


def outcome(enq: Enquiry) -> str | None:
    """What happened after the call, read from the real records: 'paid'
    (adult subscription started), 'booked' (kids free week booked)."""
    if not enq.user_id:
        return None
    if enq.offer == "kids":
        from ..models import AttendeeProfile

        ids = [a.id for a in db.session.query(AttendeeProfile).filter_by(user_id=enq.user_id)]
        if ids and db.session.query(Booking).filter(Booking.attendee_id.in_(ids)).first():
            return "booked"
        return None
    sub = (
        db.session.query(Subscription)
        .filter(
            Subscription.user_id == enq.user_id,
            Subscription.status.in_([SubscriptionStatus.active.value, SubscriptionStatus.pending.value]),
            Subscription.activated_at.isnot(None),
        )
        .first()
    )
    return "paid" if sub else None


def signup_link(enq: Enquiry) -> str:
    return absolute_url("funnel.enquiry_link", token=make_token(enq.id, SALT_ENQUIRY))


def send_admin_alert(enq: Enquiry) -> None:
    recipients = admin_alert_recipients()
    if not recipients:
        return
    o = OFFERS[enq.offer]
    html = render_template(
        "emails/admin_new_enquiry.html",
        e=enq,
        offer=o,
        age=age_on(enq.dob),
        child_age=age_on(enq.child_dob),
        ad=(__import__("app.services.marketing_report", fromlist=["ad_label"]).ad_label(enq.utm_content) if enq.utm_content else "direct"),
        calls_url=absolute_url("ops_admin.calls"),
    )
    who = enq.name + (f" (for {enq.child_first_name})" if enq.child_first_name else "")
    subject = f"Call back: {who}, {o['label'].split(':')[0]}"
    for addr in recipients:
        send_email(None, addr, subject, html, "admin_new_enquiry", enq.client_account_id)


def send_signup_link(enq: Enquiry) -> list[str]:
    """Text (if they ticked OK to text) and email (if they gave one) the
    personal sign-up link. Returns the channels used."""
    link = signup_link(enq)
    o = OFFERS[enq.offer]
    used = []
    if enq.sms_ok and len(digits(enq.phone)) >= 10:
        send_sms(
            None, enq.phone,
            f"Box2Fit: here's your link to sign up for {o['label'].split(':')[0]}: {link}",
            "payment_link", enq.client_account_id, transactional=True,
        )
        used.append("text")
    if enq.email:
        html = render_template("emails/enquiry_signup_link.html", e=enq, offer=o, link=link)
        send_email(None, enq.email, "Your Box2Fit sign-up link", html, "enquiry_signup_link", enq.client_account_id)
        used.append("email")
    enq.link_sent_at = utcnow()
    if enq.status in OPEN_STATUSES:
        enq.status = "agreed"
    return used


def remember(enq: Enquiry) -> None:
    """The person is on their way to the pay/booking page: remember who
    they are so the form is filled in and the sign-up is tied back."""
    session[SESSION_KEY] = enq.id


def current() -> Enquiry | None:
    eid = session.get(SESSION_KEY)
    return db.session.get(Enquiry, eid) if eid else None


def prefill() -> dict:
    enq = current()
    if enq is None:
        return {}
    return {
        "name": enq.name,
        "first_name": enq.first_name,
        "phone": enq.phone,
        "email": enq.email or "",
        "child_first_name": enq.child_first_name or "",
        "child_birth_year": enq.child_dob.year if enq.child_dob else None,
    }


def attach_user(user_id: int) -> None:
    """Called when the pay/booking page creates the account: ties the
    enquiry to it so the Call list can show Paid/Booked."""
    enq = current()
    if enq is None:
        return
    enq.user_id = user_id
    if enq.status in OPEN_STATUSES:
        enq.status = "agreed"
