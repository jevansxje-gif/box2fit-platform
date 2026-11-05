"""Members page sections (owner 2026-11-04): Paying, Working on, Lapsed.

Read from the real records on every page load, so people move by themselves:
a lapsed person who books, signs up again or pays comes back without anyone
touching them. Nothing here deletes or changes a member.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import (
    AttendeeProfile,
    Booking,
    BookingStatus,
    ClassInstance,
    Enquiry,
    Subscription,
    SubscriptionStatus,
    User,
)
from .tzutil import today_local

LAPSE_DAYS = 30  # no activity for this long moves a trial-only person to Lapsed
KEEP_DAYS = 30  # "Keep working" holds someone in Working on for this long

PAYING, WORKING, LAPSED = "paying", "working", "lapsed"
SECTIONS = [(PAYING, "Paying"), (WORKING, "Working on"), (LAPSED, "Lapsed")]
PAYING_STATUSES = {
    SubscriptionStatus.active.value,
    SubscriptionStatus.pending.value,
    SubscriptionStatus.past_due.value,
}


@dataclass
class MemberSection:
    section: str
    next_step: str
    next_order: int  # who needs action first, in Working on
    past_due: bool
    upcoming: date | None
    last_activity: date


def classify(
    *,
    paying: bool,
    marked_lapsed_at: datetime | None,
    keep_until: date | None,
    upcoming: date | None,
    closed: bool,
    cancelled: bool,
    last_activity: date,
    last_signal: datetime | None,
    today: date,
) -> str:
    """Which section. A manual "Mark lapsed" holds until something new
    happens after it; "Keep working" beats the rules until its date."""
    if paying:
        return PAYING
    if marked_lapsed_at and not (last_signal and last_signal > marked_lapsed_at):
        return LAPSED
    if keep_until and keep_until >= today:
        return WORKING
    if upcoming:
        return WORKING
    if closed or cancelled:
        return LAPSED
    return WORKING if last_activity >= today - timedelta(days=LAPSE_DAYS) else LAPSED


def next_step(
    *,
    upcoming: date | None,
    last_class: tuple[date, str] | None,
    any_cancelled: bool,
    enquiry: Enquiry | None,
    today: date,
) -> tuple[int, str]:
    """The one thing to do next, most urgent first (lower number)."""
    if upcoming:
        return 5, f"Booked {upcoming.strftime('%b')} {upcoming.day}"
    # A first class the gym booked by phone (Call list) that has now passed.
    phone_class = enquiry.booked_for if enquiry else None
    if phone_class and phone_class < today and (not last_class or phone_class > last_class[0]):
        if enquiry.card_sent_at:
            return 1, "Came, follow up"
        return 0, "Came, send link"
    if last_class:
        status = last_class[1]
        if status == BookingStatus.attended.value:
            return 1, "Came, follow up"  # the after-class email has gone out
        if status == BookingStatus.no_show.value:
            return 2, "No-show, rebook"
        return 0, "Mark attendance"  # the class passed and nobody marked it
    if any_cancelled:
        return 3, "Cancelled, rebook"
    if enquiry and enquiry.status == "call_back":
        return 3, "Call back"
    if enquiry and enquiry.status == "no_answer":
        return 3, "No answer, try again"
    return 4, "Never booked, call"


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a and b:
        return max(a, b)
    return a or b


def build(client_account_id: int, users: list[User]) -> dict[int, MemberSection]:
    """Section and next step for each user, without a query per person."""
    today = today_local()
    ids = [u.id for u in users]
    out: dict[int, MemberSection] = {}
    if not ids:
        return out

    subs_by_user: dict[int, list[str]] = {}
    for user_id, status in db.session.query(Subscription.user_id, Subscription.status).filter(
        Subscription.user_id.in_(ids)
    ):
        subs_by_user.setdefault(user_id, []).append(status)

    enquiries: dict[int, list[Enquiry]] = {}
    for e in (
        db.session.query(Enquiry)
        .filter(Enquiry.client_account_id == client_account_id, Enquiry.user_id.in_(ids))
        .order_by(Enquiry.created_at)
    ):
        enquiries.setdefault(e.user_id, []).append(e)

    bookings: dict[int, list[tuple]] = {}
    for user_id, bk_status, booked_at, checked_in_at, local_date in (
        db.session.query(
            AttendeeProfile.user_id, Booking.status, Booking.booked_at,
            Booking.checked_in_at, ClassInstance.local_date,
        )
        .join(Booking, Booking.attendee_id == AttendeeProfile.id)
        .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
        .filter(AttendeeProfile.user_id.in_(ids))
    ):
        bookings.setdefault(user_id, []).append((bk_status, booked_at, checked_in_at, local_date))

    for u in users:
        statuses = subs_by_user.get(u.id, [])
        enqs = enquiries.get(u.id, [])
        latest = enqs[-1] if enqs else None
        rows = bookings.get(u.id, [])

        future = [r[3] for r in rows if r[0] == BookingStatus.booked.value and r[3] >= today]
        future += [e.booked_for for e in enqs
                   if e.booked_for and e.booked_for >= today and e.status != "not_interested"]
        upcoming = min(future) if future else None

        # Newest class that has happened; on the same day (two kids) attended wins.
        marked = (BookingStatus.attended.value, BookingStatus.no_show.value)
        past = [(r[3], r[0]) for r in rows
                if r[0] in marked or (r[0] == BookingStatus.booked.value and r[3] < today)]
        past.sort(key=lambda p: (p[0], p[1] == BookingStatus.attended.value))
        last_class = past[-1] if past else None

        # Anything new a person or the gym did. Updating a Call list row counts.
        last_signal: datetime | None = None
        for r in rows:
            last_signal = _later(_later(last_signal, r[1]), r[2])
        for e in enqs:
            last_signal = _later(_later(last_signal, e.created_at), e.updated_at)

        activity = [u.created_at.date()]
        if last_signal:
            activity.append(last_signal.date())
        if last_class:
            activity.append(last_class[0])

        order, label = next_step(
            upcoming=upcoming, last_class=last_class,
            any_cancelled=any(r[0] == BookingStatus.cancelled.value for r in rows),
            enquiry=latest, today=today,
        )
        out[u.id] = MemberSection(
            section=classify(
                paying=bool(PAYING_STATUSES & set(statuses)),
                marked_lapsed_at=u.marked_lapsed_at,
                keep_until=u.keep_working_until, upcoming=upcoming,
                closed=bool(latest and latest.status == "not_interested"),
                cancelled=SubscriptionStatus.cancelled.value in statuses,
                last_activity=max(activity), last_signal=last_signal, today=today,
            ),
            next_step=label,
            next_order=order,
            past_due=SubscriptionStatus.past_due.value in statuses,
            upcoming=upcoming,
            last_activity=max(activity),
        )
    return out
