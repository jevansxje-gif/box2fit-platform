"""Celery jobs: T-24h/T-2h reminders (idempotent via sent-at stamps),
nightly schedule generation, and the no-show automark at class end + N min
(config; silent, staff-only per the RSVP policy)."""
import logging
from datetime import timedelta

from celery import shared_task
from flask import current_app, render_template

from ..extensions import db
from ..models import (
    AttendeeKind,
    Booking,
    BookingKind,
    BookingStatus,
    ClassInstance,
    ClientAccount,
    InstanceStatus,
    utcnow,
)
from ..services.booking_flow import STUDIO_ADDRESS
from ..services.messaging import send_email, send_sms
from ..services.scheduling import generate_instances
from ..services.signed_links import (
    SALT_CANCEL_BOOKING,
    SALT_CONFIRM_ATTEND,
    make_token,
)
from ..services.tzutil import fmt_local, now_utc
from ..services.urls import absolute_url

log = logging.getLogger(__name__)


@shared_task(name="app.tasks.jobs.send_due_reminders")
def send_due_reminders() -> int:
    now = now_utc()
    sent = 0
    rows = (
        db.session.query(Booking)
        .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
        .filter(
            Booking.status == BookingStatus.booked.value,
            ClassInstance.status == InstanceStatus.scheduled.value,
            ClassInstance.starts_at_utc > now,
            ClassInstance.starts_at_utc <= now + timedelta(hours=24),
        )
        .all()
    )
    for booking in rows:
        until = booking.class_instance.starts_at_utc - now
        if until <= timedelta(hours=2) and booking.reminder_2h_sent_at is None:
            _send_reminder(booking, "reminder_2h")
            booking.reminder_2h_sent_at = utcnow()
            sent += 1
        elif until <= timedelta(hours=24) and booking.reminder_24h_sent_at is None:
            _send_reminder(booking, "reminder_24h")
            booking.reminder_24h_sent_at = utcnow()
            sent += 1
    db.session.commit()
    if sent:
        log.info("reminders sent", extra={"count": sent})
    return sent


def _send_reminder(booking: Booking, template: str) -> None:
    attendee = booking.attendee
    guardian = attendee.guardian
    instance = booking.class_instance
    when = fmt_local(instance.starts_at_utc)
    cancel_url = absolute_url(
        "funnel.cancel_booking", token=make_token(booking.id, SALT_CANCEL_BOOKING)
    )
    confirm_url = absolute_url(
        "funnel.confirm_attendance",
        token=make_token(booking.id, SALT_CONFIRM_ATTEND),
    )
    is_child = attendee.kind == AttendeeKind.child.value
    is_2h = template == "reminder_2h"
    html = render_template(
        "emails/reminder.html",
        guardian=guardian,
        attendee=attendee,
        is_child=is_child,
        is_2h=is_2h,
        class_name=instance.class_type.name,
        when=when,
        address=STUDIO_ADDRESS,
        cancel_url=cancel_url,
        confirm_url=None if booking.confirmed_at else confirm_url,
    )
    who = f"{attendee.first_name}'s" if is_child else "Your"
    subject = (
        f"{who} class at Box2Fit is {'in about 2 hours' if is_2h else 'tomorrow'}"
    )
    send_email(
        guardian, guardian.email, subject, html, template,
        booking.client_account_id, attendee_id=attendee.id,
    )
    sms_body = (
        f"Box2Fit: {who.lower()} {instance.class_type.name} class is "
        f"{'in about 2 hours' if is_2h else 'tomorrow'} ({when}). "
    )
    if booking.confirmed_at is None:
        sms_body += f"Tap to check in: {confirm_url} "
    sms_body += f"Can't make it? {cancel_url}"
    send_sms(
        guardian,
        guardian.phone,
        sms_body,
        template,
        booking.client_account_id,
        attendee_id=attendee.id,
    )


@shared_task(name="app.tasks.jobs.send_trial_followups")
def send_trial_followups() -> int:
    """Day-2 nudge for attended trials that never became memberships (the
    single post-class email is easy to miss). Marketing send — honors email
    consent — and strictly once per attendee, tracked via the Message log."""
    from ..models import Message, Subscription, SubscriptionStatus
    from ..services.signed_links import SALT_ACTIVATE
    from ..services.tzutil import today_local

    target = today_local() - timedelta(days=2)
    rows = (
        db.session.query(Booking)
        .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
        .filter(
            Booking.kind.in_([BookingKind.trial.value, BookingKind.walkin.value]),
            Booking.status == BookingStatus.attended.value,
            ClassInstance.local_date == target,
        )
        .all()
    )
    sent = 0
    for b in rows:
        attendee = b.attendee
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
            continue
        already = (
            db.session.query(Message)
            .filter_by(template="trial_followup", attendee_id=attendee.id)
            .count()
        )
        if already:
            continue
        guardian = attendee.guardian
        is_child = attendee.kind == AttendeeKind.child.value
        url = absolute_url(
            "funnel.activate_membership", token=make_token(b.id, SALT_ACTIVATE)
        )
        html = render_template(
            "emails/trial_followup.html",
            guardian=guardian, attendee=attendee, is_child=is_child,
            activate_url=url,
        )
        who = f"{attendee.first_name}'s" if is_child else "Your"
        send_email(
            guardian, guardian.email,
            f"{who} spot at Box2Fit is still open",
            html, "trial_followup", b.client_account_id,
            attendee_id=attendee.id, transactional=False,
        )
        sent += 1
    db.session.commit()
    return sent


@shared_task(name="app.tasks.jobs.release_expired_waitlist_offers")
def release_expired_waitlist_offers() -> int:
    from ..services.waitlist import release_expired

    released = release_expired()
    db.session.commit()
    return released


@shared_task(name="app.tasks.jobs.drain_event_outbox")
def drain_event_outbox() -> int:
    from ..services.dispatch import drain

    done = drain()
    db.session.commit()
    return done


@shared_task(name="app.tasks.jobs.match_unmatched_calls")
def match_unmatched_calls() -> int:
    from ..services.calls import match_unmatched

    matched = match_unmatched()
    db.session.commit()
    return matched


@shared_task(name="app.tasks.jobs.generate_all_schedules")
def generate_all_schedules() -> int:
    from ..services.class_admin import prune_inactive_template_instances

    total = 0
    for client in db.session.query(ClientAccount).filter_by(active=True).all():
        total += generate_instances(client.id)
        prune_inactive_template_instances(client.id)
    db.session.commit()
    return total


@shared_task(name="app.tasks.jobs.automark_no_shows")
def automark_no_shows() -> int:
    """Unchecked bookings flip to no_show at class end + N minutes.
    Silent — surfaced in staff reporting only, never to members."""
    grace_min = current_app.config["POLICY_NOSHOW_AUTOMARK_MIN"]
    now = now_utc()
    marked = 0
    rows = (
        db.session.query(Booking)
        .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
        .filter(
            Booking.status == BookingStatus.booked.value,
            ClassInstance.status == InstanceStatus.scheduled.value,
            ClassInstance.starts_at_utc < now - timedelta(minutes=grace_min),
        )
        .all()
    )
    for booking in rows:
        ends_at = booking.class_instance.starts_at_utc + timedelta(
            minutes=booking.class_instance.duration_min + grace_min
        )
        if now >= ends_at:
            booking.status = BookingStatus.no_show.value
            booking.attendance_marked_by = "auto"
            marked += 1
    db.session.commit()
    if marked:
        log.info("no-shows automarked", extra={"count": marked})
    return marked
