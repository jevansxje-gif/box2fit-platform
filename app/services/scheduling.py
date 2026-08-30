"""Recurring schedule generation and live availability.

Templates are weekly (local weekday + local time); a Celery beat job (and the
seed script) materializes ClassInstance rows N weeks ahead, skipping closure
dates. Availability counts active bookings against instance capacity.
"""
import logging
from datetime import date, timedelta

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import (
    AttendeeProfile,
    Booking,
    BookingStatus,
    ClassInstance,
    ClassType,
    ClosureDate,
    InstanceStatus,
    ScheduleTemplate,
)
from .tzutil import local_to_utc, now_utc, today_local

log = logging.getLogger(__name__)


def generate_instances(client_account_id: int, weeks: int | None = None) -> int:
    """Materialize class instances from active templates. Idempotent — the
    (template_id, local_date) unique key means re-runs only fill gaps."""
    weeks = weeks or current_app.config["SCHEDULE_HORIZON_WEEKS"]
    start = today_local()
    end = start + timedelta(weeks=weeks)

    closures = {
        c.closed_on
        for c in db.session.query(ClosureDate)
        .filter(ClosureDate.client_account_id == client_account_id)
        .all()
    }
    templates = (
        db.session.query(ScheduleTemplate)
        .filter(
            ScheduleTemplate.client_account_id == client_account_id,
            ScheduleTemplate.active.is_(True),
        )
        .all()
    )
    existing = {
        (i.template_id, i.local_date)
        for i in db.session.query(ClassInstance)
        .filter(
            ClassInstance.client_account_id == client_account_id,
            ClassInstance.local_date >= start,
            ClassInstance.local_date <= end,
            ClassInstance.template_id.isnot(None),
        )
        .all()
    }

    created = 0
    d = start
    while d <= end:
        if d not in closures:
            for tpl in templates:
                if tpl.weekday != d.weekday() or (tpl.id, d) in existing:
                    continue
                if tpl.starts_on and d < tpl.starts_on:
                    continue  # program hasn't launched yet
                ct = tpl.class_type
                db.session.add(
                    ClassInstance(
                        client_account_id=client_account_id,
                        template_id=tpl.id,
                        class_type_id=ct.id,
                        cohort_label=tpl.cohort_label,
                        trainer_id=tpl.trainer_id,
                        starts_at_utc=local_to_utc(d, tpl.start_time_local),
                        local_date=d,
                        local_time=tpl.start_time_local,
                        duration_min=tpl.duration_min or ct.duration_min,
                        capacity=tpl.capacity or ct.default_capacity,
                        accepts_trials=(
                            tpl.accepts_trials
                            if tpl.accepts_trials is not None
                            else ct.accepts_trials
                        ),
                    )
                )
                created += 1
        d += timedelta(days=1)
    if created:
        log.info("schedule instances generated", extra={"count": created})
    return created


def booked_counts(instance_ids: list[int]) -> dict[int, int]:
    if not instance_ids:
        return {}
    rows = (
        db.session.query(Booking.class_instance_id, func.count(Booking.id))
        .filter(
            Booking.class_instance_id.in_(instance_ids),
            Booking.status.in_(
                [BookingStatus.booked.value, BookingStatus.attended.value]
            ),
        )
        .group_by(Booking.class_instance_id)
        .all()
    )
    return dict(rows)


def upcoming_instances(
    client_account_id: int,
    segment_tag: str | None = None,
    class_type_id: int | None = None,
    days: int = 14,
    trials_only: bool = False,
) -> list[dict]:
    from sqlalchemy import or_

    q = (
        db.session.query(ClassInstance)
        .join(ClassType, ClassInstance.class_type_id == ClassType.id)
        .outerjoin(ScheduleTemplate, ClassInstance.template_id == ScheduleTemplate.id)
        .filter(
            ClassInstance.client_account_id == client_account_id,
            ClassInstance.status == InstanceStatus.scheduled.value,
            ClassInstance.starts_at_utc > now_utc(),
            ClassInstance.local_date <= today_local() + timedelta(days=days),
            ClassType.active.is_(True),
            # instances of a deactivated weekly template never show
            or_(
                ClassInstance.template_id.is_(None),
                ScheduleTemplate.active.is_(True),
            ),
        )
    )
    if segment_tag:
        q = q.filter(ClassType.segment_tag == segment_tag)
    if class_type_id:
        q = q.filter(ClassInstance.class_type_id == class_type_id)
    if trials_only:
        q = q.filter(ClassInstance.accepts_trials.is_(True))
    instances = q.order_by(ClassInstance.starts_at_utc).all()

    counts = booked_counts([i.id for i in instances])
    return [
        {
            "instance": inst,
            "remaining": max(0, inst.capacity - counts.get(inst.id, 0)),
        }
        for inst in instances
    ]


def validate_bookable(
    instance: ClassInstance | None,
    attendee: AttendeeProfile | None = None,
    for_trial: bool = False,
) -> str | None:
    """Server-side booking validation. Returns an error string or None.
    Booking is open until class start (RSVP policy) unless a cutoff config
    is set. Youth brackets validate the attendee's age."""
    if instance is None or instance.status != InstanceStatus.scheduled.value:
        return "That class is no longer available."

    cutoff_min = current_app.config["POLICY_BOOKING_CUTOFF_MIN"]
    seconds_to_start = (instance.starts_at_utc - now_utc()).total_seconds()
    if seconds_to_start <= cutoff_min * 60:
        return "That class has already started."

    if for_trial and not instance.accepts_trials:
        return "That class doesn't accept free-trial bookings."

    remaining = instance.capacity - booked_counts([instance.id]).get(instance.id, 0)
    if remaining <= 0:
        return "That class is full."

    if attendee is not None:
        err = validate_age(instance.class_type, attendee)
        if err:
            return err
        # Past-due blocks NEW bookings (resolved policy). Existing bookings
        # are honored; booking re-enables the moment payment succeeds.
        from .billing import guardian_is_past_due

        if guardian_is_past_due(attendee.user_id):
            return (
                "A quick card update is needed before booking — check your "
                "email for the one-tap update link."
            )
    return None


def validate_age(class_type: ClassType, attendee: AttendeeProfile) -> str | None:
    if not class_type.is_youth:
        return None
    age = attendee.age_in_year(today_local().year)
    if age is None:
        return "Please provide the attendee's birth year."
    if class_type.age_min is not None and age < class_type.age_min:
        return (
            f"{class_type.name} is for {class_type.age_bracket_label().lower()} — "
            f"this attendee is younger. Pick the right age group below."
        )
    if class_type.age_max is not None and age > class_type.age_max:
        return (
            f"{class_type.name} is for {class_type.age_bracket_label().lower()} — "
            f"this attendee is older. Pick the right age group below."
        )
    return None
