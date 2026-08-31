"""Add-to-calendar for bookings: a Google Calendar prefill URL and a
standards-compliant .ics file (Apple/Outlook/everything else)."""
from datetime import timedelta
from urllib.parse import quote

from ..models import Booking
from .booking_flow import STUDIO_ADDRESS
from .signed_links import SALT_CANCEL_BOOKING, make_token
from .urls import absolute_url

SALT_CALENDAR = "calendar-ics"


def _fmt_utc(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _event_fields(booking: Booking) -> dict:
    inst = booking.class_instance
    start = inst.starts_at_utc
    end = start + timedelta(minutes=inst.duration_min)
    who = booking.attendee.first_name
    title = f"{inst.class_type.name} — Box2Fit ({who})"
    cancel_url = absolute_url(
        "funnel.cancel_booking", token=make_token(booking.id, SALT_CANCEL_BOOKING)
    )
    description = (
        f"{who}'s class at Box2Fit White Rock. Arrive 10 minutes early; "
        f"free underground parking at the building; gloves and wraps "
        f"provided. Need to cancel? {cancel_url}"
    )
    return {
        "start": start,
        "end": end,
        "title": title,
        "description": description,
        "location": STUDIO_ADDRESS,
    }


def google_calendar_url(booking: Booking) -> str:
    f = _event_fields(booking)
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(f['title'])}"
        f"&dates={_fmt_utc(f['start'])}/{_fmt_utc(f['end'])}"
        f"&location={quote(f['location'])}"
        f"&details={quote(f['description'])}"
    )


def ics_download_url(booking: Booking) -> str:
    from .signed_links import make_token as _mk

    return absolute_url(
        "funnel.booking_ics", token=_mk(booking.id, SALT_CALENDAR)
    )


def ics_content(booking: Booking) -> str:
    f = _event_fields(booking)

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")

    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Box2Fit//Platform//EN",
            "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:booking-{booking.id}@health.box2fit.com",
            f"DTSTAMP:{_fmt_utc(f['start'])}",
            f"DTSTART:{_fmt_utc(f['start'])}",
            f"DTEND:{_fmt_utc(f['end'])}",
            f"SUMMARY:{esc(f['title'])}",
            f"DESCRIPTION:{esc(f['description'])}",
            f"LOCATION:{esc(f['location'])}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
