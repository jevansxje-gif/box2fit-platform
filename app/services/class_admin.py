"""Class lifecycle administration shared by the ops UI and scheduled jobs:
cancelling a class with member notification, and pruning the calendar when
weekly templates are deactivated."""
import logging

from flask import render_template

from ..extensions import db
from ..models import (
    BookingStatus,
    ClassInstance,
    InstanceStatus,
    ScheduleTemplate,
    WaitlistEntry,
    utcnow,
)
from .messaging import send_email, send_sms
from .tzutil import fmt_local, today_local

log = logging.getLogger(__name__)


def cancel_class(inst: ClassInstance) -> None:
    """Cancel one class: notify booked members (email + SMS) with the nearest
    equivalent class offered, release bookings, dissolve the waitlist."""
    inst.status = InstanceStatus.cancelled.value
    when = fmt_local(inst.starts_at_utc)
    alt = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.client_account_id == inst.client_account_id,
            ClassInstance.class_type_id == inst.class_type_id,
            ClassInstance.status == InstanceStatus.scheduled.value,
            ClassInstance.starts_at_utc > inst.starts_at_utc,
            ClassInstance.id != inst.id,
        )
        .order_by(ClassInstance.starts_at_utc)
        .first()
    )
    alt_when = fmt_local(alt.starts_at_utc) if alt else None
    for b in inst.bookings:
        if b.status != BookingStatus.booked.value:
            continue
        b.status = BookingStatus.cancelled.value
        b.cancelled_at = utcnow()
        guardian = b.attendee.guardian
        send_email(
            guardian, guardian.email,
            f"Class cancelled: {inst.class_type.name} ({when})",
            render_template(
                "emails/class_cancelled.html",
                guardian=guardian,
                attendee=b.attendee,
                class_name=inst.class_type.name,
                when=when,
                alt_when=alt_when,
            ),
            "class_cancelled", inst.client_account_id, attendee_id=b.attendee_id,
        )
        send_sms(
            guardian, guardian.phone,
            f"Box2Fit: {inst.class_type.name} on {when} is cancelled — sorry! "
            + (
                f"Nearest equivalent: {alt_when}. Book from your account."
                if alt_when
                else "We'll see you at the next one."
            ),
            "class_cancelled", inst.client_account_id, attendee_id=b.attendee_id,
        )
    for w in (
        db.session.query(WaitlistEntry)
        .filter(
            WaitlistEntry.class_instance_id == inst.id,
            WaitlistEntry.status.in_(["waiting", "offered"]),
        )
        .all()
    ):
        w.status = "released"


def remove_future_instances(template_id: int) -> tuple[int, int]:
    """Future classes of a template leave the calendar: booked ones cancel
    WITH member notification, empty ones are deleted (kept-but-cancelled if
    historical bookings reference them). Returns (removed, had_bookings)."""
    future = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.template_id == template_id,
            ClassInstance.local_date >= today_local(),
            ClassInstance.status == InstanceStatus.scheduled.value,
        )
        .all()
    )
    removed = cancelled = 0
    for inst in future:
        has_bookings = any(
            b.status == BookingStatus.booked.value for b in inst.bookings
        )
        if has_bookings:
            cancel_class(inst)
            cancelled += 1
        else:
            for w in db.session.query(WaitlistEntry).filter_by(
                class_instance_id=inst.id
            ):
                w.status = "released"
            if inst.bookings:  # historical rows reference it — keep, cancelled
                inst.status = InstanceStatus.cancelled.value
            else:
                db.session.delete(inst)
        removed += 1
    return removed, cancelled


def reschedule_template(tpl: ScheduleTemplate, new_weekday: int, new_start) -> int:
    """Move a weekly class to a new day/time. Future occurrences MOVE with
    their bookings intact; booked members get a schedule-change email. An
    occurrence whose new time would already be in the past is cancelled
    (with the usual notification) instead. Returns moved count."""
    from datetime import timedelta as _td

    from .tzutil import local_to_utc, now_utc

    delta_days = new_weekday - tpl.weekday
    changed = (
        tpl.weekday != new_weekday or tpl.start_time_local != new_start
    )
    if not changed:
        return 0
    tpl.weekday = new_weekday
    tpl.start_time_local = new_start

    moved = 0
    future = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.template_id == tpl.id,
            ClassInstance.local_date >= today_local(),
            ClassInstance.status == InstanceStatus.scheduled.value,
        )
        .all()
    )
    for inst in future:
        old_desc = fmt_local(inst.starts_at_utc)
        new_date = inst.local_date + _td(days=delta_days)
        new_utc = local_to_utc(new_date, new_start)
        if new_utc <= now_utc():
            cancel_class(inst)
            continue
        inst.local_date = new_date
        inst.local_time = new_start
        inst.starts_at_utc = new_utc
        moved += 1
        for b in inst.bookings:
            if b.status != BookingStatus.booked.value:
                continue
            guardian = b.attendee.guardian
            new_desc = fmt_local(new_utc)
            send_email(
                guardian, guardian.email,
                f"Schedule change: {inst.class_type.name} moved to {new_desc}",
                render_template(
                    "emails/schedule_change.html",
                    guardian=guardian,
                    attendee=b.attendee,
                    class_name=inst.class_type.name,
                    old_when=old_desc,
                    new_when=new_desc,
                ),
                "schedule_change", inst.client_account_id,
                attendee_id=b.attendee_id,
            )
            send_sms(
                guardian, guardian.phone,
                f"Box2Fit: {inst.class_type.name} moved from {old_desc} to "
                f"{new_desc}. Your booking moved with it — reply or cancel "
                "from your account if it doesn't work.",
                "schedule_change", inst.client_account_id,
                attendee_id=b.attendee_id,
            )
    return moved


def prune_inactive_template_instances(client_account_id: int) -> int:
    """Sweep: remove lingering future occurrences of EVERY inactive template
    (catches instances generated before a deactivation). Runs nightly with
    schedule generation and on the Builder's regenerate action."""
    total = 0
    inactive = (
        db.session.query(ScheduleTemplate)
        .filter_by(client_account_id=client_account_id, active=False)
        .all()
    )
    for tpl in inactive:
        removed, _ = remove_future_instances(tpl.id)
        total += removed
    if total:
        log.info("pruned instances of inactive templates", extra={"count": total})
    return total
