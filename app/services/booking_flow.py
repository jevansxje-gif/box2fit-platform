"""The funnel booking transaction: guardian lightweight account + child
attendee + guardian-signed waiver + booking + lead, plus outbox events and
confirmation messages. Kept out of the blueprint so the api blueprint (and
later the mobile app) reuses the exact same path."""
import logging

from flask import render_template
from sqlalchemy import func

from ..extensions import db
from ..models import (
    AttendeeKind,
    AttendeeProfile,
    Booking,
    BookingKind,
    BookingStatus,
    ClassInstance,
    Lead,
    LeadStatus,
    Role,
    User,
    WaiverDocument,
    WaiverSignature,
    utcnow,
)
from .messaging import send_email, send_sms
from .signed_links import SALT_CANCEL_BOOKING, make_token
from .tracking import enqueue_event
from .tzutil import fmt_local
from .urls import absolute_url

log = logging.getLogger(__name__)

STUDIO_ADDRESS = "1522 Finlay St, Unit 216, White Rock, BC"


def get_or_create_guardian(
    client_account_id: int, name: str, email: str, phone: str,
    consent_email: bool, consent_sms: bool,
) -> User:
    """Trial bookers get a lightweight account at booking (no password yet)
    so their journey is continuous into the portal."""
    user = (
        db.session.query(User)
        .filter_by(client_account_id=client_account_id, email=email.lower())
        .one_or_none()
    )
    if user is None:
        user = User(
            client_account_id=client_account_id,
            email=email.lower(),
            name=name,
            phone=phone,
            role=Role.member.value,
        )
        db.session.add(user)
        db.session.flush()
    else:
        user.name = name or user.name
        user.phone = phone or user.phone
    if consent_email or consent_sms:
        user.consent_email = user.consent_email or consent_email
        user.consent_sms = user.consent_sms or consent_sms
        if user.consent_captured_at is None:
            user.consent_captured_at = utcnow()
    return user


def create_child_attendee(
    guardian: User,
    first_name: str,
    birth_year: int,
    emergency_contact_name: str,
    emergency_contact_phone: str,
    health_answers: dict,
) -> AttendeeProfile:
    # Same child re-submitted (double-tapped Continue on a slow response, or
    # a genuine second signup) must not mint a duplicate profile — seen in
    # production 2026-08-29: four identical children in one minute.
    attendee = (
        db.session.query(AttendeeProfile)
        .filter(
            AttendeeProfile.user_id == guardian.id,
            AttendeeProfile.kind == AttendeeKind.child.value,
            func.lower(AttendeeProfile.first_name) == first_name.lower(),
            AttendeeProfile.birth_year == birth_year,
        )
        .first()
    )
    if attendee:
        attendee.emergency_contact_name = (
            emergency_contact_name or attendee.emergency_contact_name
        )
        attendee.emergency_contact_phone = (
            emergency_contact_phone or attendee.emergency_contact_phone
        )
        attendee.health_json = health_answers or attendee.health_json
        return attendee
    attendee = AttendeeProfile(
        client_account_id=guardian.client_account_id,
        user_id=guardian.id,
        kind=AttendeeKind.child.value,
        first_name=first_name,
        birth_year=birth_year,
        emergency_contact_name=emergency_contact_name,
        emergency_contact_phone=emergency_contact_phone,
        health_json=health_answers,
    )
    db.session.add(attendee)
    db.session.flush()
    return attendee


def create_self_attendee(guardian: User, health_answers: dict) -> AttendeeProfile:
    existing = (
        db.session.query(AttendeeProfile)
        .filter_by(user_id=guardian.id, kind=AttendeeKind.self_.value)
        .one_or_none()
    )
    if existing:
        existing.health_json = health_answers or existing.health_json
        return existing
    attendee = AttendeeProfile(
        client_account_id=guardian.client_account_id,
        user_id=guardian.id,
        kind=AttendeeKind.self_.value,
        first_name=guardian.name.split(" ")[0],
        last_name=" ".join(guardian.name.split(" ")[1:]) or None,
        health_json=health_answers,
    )
    db.session.add(attendee)
    db.session.flush()
    return attendee


def sign_waiver(attendee: AttendeeProfile, guardian: User, signature_name: str) -> WaiverSignature:
    kind = "minor" if attendee.kind == AttendeeKind.child.value else "adult"
    doc = (
        db.session.query(WaiverDocument)
        .filter_by(
            client_account_id=guardian.client_account_id, kind=kind, active=True
        )
        .order_by(WaiverDocument.version.desc())
        .first()
    )
    if doc is None:  # never block a booking on missing document seed
        doc = WaiverDocument(
            client_account_id=guardian.client_account_id,
            kind=kind,
            version=1,
            body_md="(waiver text pending — supplied at launch)",
        )
        db.session.add(doc)
        db.session.flush()
    sig = WaiverSignature(
        attendee_id=attendee.id,
        document_id=doc.id,
        signed_by_user_id=guardian.id,
        signature_name=signature_name,
    )
    db.session.add(sig)
    return sig


def create_trial_booking(
    instance: ClassInstance,
    attendee: AttendeeProfile,
    lead: Lead,
    walkin: bool = False,
) -> tuple[Booking, bool]:
    """Returns (booking, created). A live booking for the same attendee and
    class is reused, so a re-submitted form can't double-book — the caller
    skips confirmation/alert emails when created is False."""
    existing = (
        db.session.query(Booking)
        .filter(
            Booking.attendee_id == attendee.id,
            Booking.class_instance_id == instance.id,
            Booking.status.in_(
                [BookingStatus.booked.value, BookingStatus.attended.value]
            ),
        )
        .first()
    )
    if existing:
        return existing, False
    booking = Booking(
        client_account_id=instance.client_account_id,
        attendee_id=attendee.id,
        class_instance_id=instance.id,
        lead_id=lead.id,
        kind=BookingKind.walkin.value if walkin else BookingKind.trial.value,
        ipad_walkin=walkin,
    )
    db.session.add(booking)
    db.session.flush()
    lead.status = LeadStatus.booked.value
    enqueue_event("Lead", instance.client_account_id, lead)
    enqueue_event(
        "Schedule", instance.client_account_id, lead, booking_id=booking.id
    )
    return booking, True


def admin_alert_recipients() -> list[str]:
    """Staff alert mailboxes: the ops-managed list (site setting), falling
    back to the ADMIN_NOTIFY_EMAIL env value if the list was never set."""
    from flask import current_app

    from ..models import SiteSetting

    raw = SiteSetting.get("admin_notify_emails", "")
    emails = [e.strip() for e in raw.split(",") if e.strip()]
    if emails:
        return emails
    env = current_app.config["ADMIN_NOTIFY_EMAIL"]
    return [env] if env else []


def send_admin_signup_alert(booking: Booking) -> None:
    """New free-class sign-up → email every configured staff mailbox."""
    recipients = admin_alert_recipients()
    if not recipients:
        return
    attendee = booking.attendee
    guardian = attendee.guardian
    instance = booking.class_instance
    is_child = attendee.kind == AttendeeKind.child.value
    html = render_template(
        "emails/admin_new_signup.html",
        attendee=attendee,
        guardian=guardian,
        is_child=is_child,
        class_name=instance.class_type.name,
        cohort=instance.cohort_label,
        when=fmt_local(instance.starts_at_utc),
        ops_url=absolute_url("ops.today"),
    )
    subject = (
        f"New sign-up: {attendee.first_name}"
        + (f" (child of {guardian.name})" if is_child else f" ({guardian.name})")
        + f" — {instance.class_type.name} {fmt_local(instance.starts_at_utc, '%a %b %d')}"
    )
    for admin_email in recipients:
        send_email(
            None, admin_email, subject, html, "admin_new_signup",
            booking.client_account_id, attendee_id=attendee.id,
        )


def send_booking_confirmation(booking: Booking) -> None:
    from .calendar_links import google_calendar_url, ics_download_url

    attendee = booking.attendee
    guardian = attendee.guardian
    instance = booking.class_instance
    when = fmt_local(instance.starts_at_utc)
    cancel_url = absolute_url(
        "funnel.cancel_booking", token=make_token(booking.id, SALT_CANCEL_BOOKING)
    )
    is_child = attendee.kind == AttendeeKind.child.value
    html = render_template(
        "emails/booking_confirmation.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        class_name=instance.class_type.name,
        when=when,
        address=STUDIO_ADDRESS,
        cancel_url=cancel_url,
        gcal_url=google_calendar_url(booking),
        ics_url=ics_download_url(booking),
    )
    subject = (
        f"{attendee.first_name} is booked — free first class at Box2Fit"
        if is_child
        else "You're booked — free first class at Box2Fit"
    )
    send_email(
        guardian, guardian.email, subject, html, "booking_confirmation",
        booking.client_account_id, attendee_id=attendee.id,
    )
    who = f"{attendee.first_name}'s" if is_child else "your"
    send_sms(
        guardian,
        guardian.phone,
        f"Box2Fit: {who} free first class is booked. {instance.class_type.name}, "
        f"{when}. {STUDIO_ADDRESS}. Can't make it? {cancel_url}",
        "booking_confirmation",
        booking.client_account_id,
        attendee_id=attendee.id,
    )
