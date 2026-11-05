"""Unsubscribing from marketing email (CASL). Bookings, billing and other
service messages carry on; only marketing stops."""
from __future__ import annotations

from ..models import User, utcnow
from .signed_links import SALT_UNSUBSCRIBE, make_token, read_token
from .urls import absolute_url

# An unsubscribe link has to keep working long after the email went out.
LINK_LIFETIME = 60 * 60 * 24 * 365 * 10


def unsubscribe_url(user: User | None) -> str:
    """A link for this person. With no user (a staff test send) it goes to the
    page but can't unsubscribe anyone."""
    return absolute_url(
        "funnel.unsubscribe", token=make_token(user.id if user else 0, SALT_UNSUBSCRIBE)
    )


def user_id_from(token: str) -> int | None:
    return read_token(token, SALT_UNSUBSCRIBE, max_age=LINK_LIFETIME)


def apply(user: User) -> None:
    user.consent_email = False
    user.notify_prefs = {**(user.notify_prefs or {}), "news": False}
    if user.unsubscribed_at is None:
        user.unsubscribed_at = utcnow()
