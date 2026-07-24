"""Waitlist with auto-promotion. When a spot opens, the first in line is
auto-booked and notified with a confirm-or-release window (default 2h) so
spots don't die in unread notifications. Expired offers release the spot and
promote the next entry."""
import logging
from datetime import timedelta

from flask import current_app, render_template

from ..extensions import db
from ..models import (
    AttendeeProfile,
    Booking,
    BookingKind,
    BookingStatus,
    ClassInstance,
    WaitlistEntry,
    utcnow,
)
from .messaging import send_email, send_sms
from .scheduling import booked_counts
from .signed_links import SALT_WAITLIST_CONFIRM, make_token
from .tzutil import fmt_local, now_utc
from .urls import absolute_url

log = logging.getLogger(__name__)


def join(instance: ClassInstance, attendee: AttendeeProfile) -> WaitlistEntry | None:
    existing = (
        db.session.query(WaitlistEntry)
        .filter(
            WaitlistEntry.class_instance_id == instance.id,
            WaitlistEntry.attendee_id == attendee.id,
            WaitlistEntry.status.in_(["waiting", "offered", "confirmed"]),
        )
        .first()
    )
    if existing:
        return existing
    position = (
        db.session.query(WaitlistEntry)
        .filter_by(class_instance_id=instance.id, status="waiting")
        .count()
        + 1
    )
    entry = WaitlistEntry(
        client_account_id=instance.client_account_id,
        class_instance_id=instance.id,
        attendee_id=attendee.id,
        position=position,
    )
    db.session.add(entry)
    return entry


def promote_next(instance: ClassInstance) -> WaitlistEntry | None:
    """Called when a spot opens (cancel). Auto-books the first waiting entry
    and starts the confirm-or-release countdown."""
    remaining = instance.capacity - booked_counts([instance.id]).get(instance.id, 0)
    if remaining <= 0 or instance.starts_at_utc <= now_utc():
        return None
    entry = (
        db.session.query(WaitlistEntry)
        .filter_by(class_instance_id=instance.id, status="waiting")
        .order_by(WaitlistEntry.position, WaitlistEntry.id)
        .first()
    )
    if entry is None:
        return None
    attendee = db.session.get(AttendeeProfile, entry.attendee_id)
    booking = (
        db.session.query(Booking)
        .filter_by(attendee_id=attendee.id, class_instance_id=instance.id)
        .one_or_none()
    )
    if booking is None:
        booking = Booking(
            client_account_id=instance.client_account_id,
            attendee_id=attendee.id,
            class_instance_id=instance.id,
            kind=BookingKind.member.value,
        )
        db.session.add(booking)
    else:
        booking.status = BookingStatus.booked.value
        booking.cancelled_at = None
    hours = current_app.config["POLICY_WAITLIST_CONFIRM_HOURS"]
    entry.status = "offered"
    entry.offered_at = utcnow()
    entry.expires_at = utcnow() + timedelta(hours=hours)
    db.session.flush()
    _notify_promotion(entry, booking)
    return entry


def confirm(entry: WaitlistEntry) -> None:
    if entry.status == "offered":
        entry.status = "confirmed"


def release_expired() -> int:
    """Beat task: expired offers → release the spot, promote the next."""
    released = 0
    expired = (
        db.session.query(WaitlistEntry)
        .filter(
            WaitlistEntry.status == "offered",
            WaitlistEntry.expires_at < utcnow(),
        )
        .all()
    )
    for entry in expired:
        entry.status = "released"
        booking = (
            db.session.query(Booking)
            .filter_by(
                attendee_id=entry.attendee_id,
                class_instance_id=entry.class_instance_id,
                status=BookingStatus.booked.value,
            )
            .one_or_none()
        )
        if booking:
            booking.status = BookingStatus.cancelled.value
            booking.cancelled_at = utcnow()
        released += 1
        promote_next(db.session.get(ClassInstance, entry.class_instance_id))
    if released:
        log.info("waitlist offers released", extra={"count": released})
    return released


def _notify_promotion(entry: WaitlistEntry, booking: Booking) -> None:
    attendee = booking.attendee or db.session.get(AttendeeProfile, entry.attendee_id)
    guardian = attendee.guardian
    instance = db.session.get(ClassInstance, entry.class_instance_id)
    when = fmt_local(instance.starts_at_utc)
    confirm_url = absolute_url(
        "portal.waitlist_confirm", token=make_token(entry.id, SALT_WAITLIST_CONFIRM)
    )
    hours = current_app.config["POLICY_WAITLIST_CONFIRM_HOURS"]
    is_child = attendee.kind == "child"
    who = f"{attendee.first_name} is" if is_child else "You're"
    html = render_template(
        "emails/waitlist_promoted.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        class_name=instance.class_type.name,
        when=when,
        hours=hours,
        confirm_url=confirm_url,
    )
    send_email(
        guardian, guardian.email,
        f"A spot opened up — {who.lower()} in ({when})",
        html, "waitlist_promoted", entry.client_account_id,
        attendee_id=attendee.id,
    )
    send_sms(
        guardian, guardian.phone,
        f"Box2Fit: a spot opened in {instance.class_type.name} ({when}) and "
        f"{who.lower()} booked! Tap to confirm within {hours}h or the spot "
        f"passes to the next family: {confirm_url}",
        "waitlist_promoted", entry.client_account_id, attendee_id=attendee.id,
    )
