"""Opens and clicks on payment emails, read from Brevo's event log (owner
2026-11-04) for the Members page's Awaiting payment section. Pulled when that
section is viewed, at most every few minutes. A Brevo failure only leaves the
last known status on screen; it never breaks the page."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..extensions import db
from ..models import Message, SiteSetting, utcnow

log = logging.getLogger(__name__)

# Emails that ask someone to add a card or start paying.
PAYMENT_TEMPLATES = (
    "post_class",  # after a trial class: start the membership
    "trial_followup",  # the reminder of that
    "enquiry_card_link",  # Call list: add your card
    "enquiry_signup_link",  # Call list: sign-up link
    "payment_link",  # staff-sent membership link
)
SYNC_EVERY = timedelta(minutes=5)
SYNC_KEY = "brevo_events_synced_at"
LOOKBACK_DAYS = 90  # the most Brevo allows
PAGE = 2500
MAX_PAGES = 6
NOT_DELIVERED = {"bounces", "hardBounces", "softBounces", "blocked", "invalid", "error"}


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _fetch_events() -> list[dict]:
    import requests

    events: list[dict] = []
    for page in range(MAX_PAGES):
        resp = requests.get(
            "https://api.brevo.com/v3/smtp/statistics/events",
            headers={"api-key": current_app.config["BREVO_API_KEY"], "accept": "application/json"},
            params={"days": LOOKBACK_DAYS, "limit": PAGE, "offset": page * PAGE},
            timeout=8,
        )
        resp.raise_for_status()
        batch = (resp.json() or {}).get("events") or []
        events.extend(batch)
        if len(batch) < PAGE:
            break
    return events


def apply_events(messages: list[Message], events: list[dict]) -> None:
    """Brevo matches on address and subject: each event belongs to the newest
    matching email sent before it. Keeps the first time of each event."""
    by_key: dict[tuple[str, str], list[Message]] = {}
    for m in sorted(messages, key=lambda x: x.sent_at):
        by_key.setdefault((m.recipient.lower(), m.subject or ""), []).append(m)

    field_for = {"delivered": "delivered_at", "opened": "opened_at", "clicks": "clicked_at"}
    for ev in events:
        kind = ev.get("event")
        if kind not in field_for and kind not in NOT_DELIVERED:
            continue
        sent = by_key.get(((ev.get("email") or "").lower(), ev.get("subject") or ""))
        when = _parse(ev.get("date"))
        if not sent or not when:
            continue
        msg = next((m for m in reversed(sent) if m.sent_at <= when + timedelta(minutes=5)), None)
        if msg is None:
            continue
        field = field_for.get(kind, "failed_at")
        current = getattr(msg, field)
        if current is None or when < current:
            setattr(msg, field, when)


def sync(client_account_id: int, force: bool = False) -> bool:
    """Refresh the status. False only if Brevo couldn't be read just now."""
    if not current_app.config.get("BREVO_API_KEY"):
        return True
    last = _parse(SiteSetting.get(SYNC_KEY))
    if not force and last and utcnow() - last < SYNC_EVERY:
        return True
    messages = (
        db.session.query(Message)
        .filter(
            Message.client_account_id == client_account_id,
            Message.channel == "email",
            Message.template.in_(PAYMENT_TEMPLATES),
            Message.delivery_status == "sent",
            Message.sent_at >= utcnow() - timedelta(days=LOOKBACK_DAYS + 1),
        )
        .all()
    )
    try:
        apply_events(messages, _fetch_events())
    except Exception:
        log.exception("couldn't read Brevo email events")
        return False
    SiteSetting.set(SYNC_KEY, utcnow().isoformat())
    db.session.commit()
    return True


def email_status(emails: list[Message]) -> tuple[str, str, datetime | None]:
    """The furthest anyone got across their payment emails: label, chip tone,
    when. A click on an earlier email still counts."""
    for field, label, tone in (
        ("clicked_at", "Clicked", "success"),
        ("opened_at", "Opened", "info"),
    ):
        times = [getattr(m, field) for m in emails if getattr(m, field)]
        if times:
            return label, tone, max(times)
    # Nothing opened: judge by the newest email only.
    newest = max(emails, key=lambda m: m.sent_at)
    if newest.delivered_at:
        return "Delivered, not opened", "warn", newest.delivered_at
    if newest.failed_at:
        return "Not delivered", "red", newest.failed_at
    return "Sent", "warn", newest.sent_at
