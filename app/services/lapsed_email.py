"""Marketing email to the Members page's Lapsed section (owner 2026-11-05).

Canada's anti-spam law (CASL): the opt-in flags can't be trusted for people who
signed up before 2026-11-05 (some forms set them with no tick box), so this
doesn't rely on them. It uses implied consent, worked out per person:
a purchase in the last 2 years, or asking about classes in the last 6 months.
Anyone unsubscribed or marked not interested is always left out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask import render_template

from ..extensions import db
from ..models import Enquiry, Message, Payment, Role, User, utcnow
from . import member_sections as ms
from .messaging import send_email
from .unsubscribe import unsubscribe_url

TEMPLATE = "lapsed_offer"
INQUIRY_WINDOW = timedelta(days=181)  # six months, on the short side
PURCHASE_WINDOW = timedelta(days=730)  # two years


@dataclass
class Person:
    user: User
    eligible: bool
    reason: str  # why they can be emailed, or why they're left out


def _why(
    user: User,
    section: ms.MemberSection,
    last_paid: datetime | None,
    last_asked: datetime,
    already_sent: bool,
    now: datetime,
) -> tuple[bool, str]:
    if user.unsubscribed_at:
        return False, "Unsubscribed"
    if not user.active:
        return False, "Account switched off"
    if section.not_interested:
        return False, "Said not interested"
    if already_sent:
        return False, "Already sent this email"
    if last_paid and now - last_paid <= PURCHASE_WINDOW:
        return True, f"Paid on {_day(last_paid)}"
    if now - last_asked <= INQUIRY_WINDOW:
        return True, f"Asked on {_day(last_asked)}, can email until {_day(last_asked + INQUIRY_WINDOW)}"
    return False, f"Over 6 months since they asked ({_day(last_asked)})"


def _day(value: datetime | date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def audience(client_account_id: int, subject: str = "") -> list[Person]:
    """Everyone in Lapsed, each marked in or out with the reason. Eligible first."""
    users = (
        db.session.query(User)
        .filter_by(client_account_id=client_account_id, role=Role.member.value)
        .all()
    )
    info = ms.build(client_account_id, users)
    lapsed = [u for u in users if info[u.id].section == ms.LAPSED]
    ids = [u.id for u in lapsed] or [0]

    last_paid: dict[int, datetime] = {}
    for user_id, created in db.session.query(Payment.user_id, Payment.created_at).filter(
        Payment.user_id.in_(ids), Payment.status == "paid", Payment.amount_cents > 0
    ):
        last_paid[user_id] = max(created, last_paid.get(user_id, created))

    last_asked = {u.id: u.created_at for u in lapsed}
    for user_id, created in db.session.query(Enquiry.user_id, Enquiry.created_at).filter(
        Enquiry.user_id.in_(ids)
    ):
        last_asked[user_id] = max(created, last_asked[user_id])

    sent_to = set()
    if subject:
        sent_to = {
            row[0] for row in db.session.query(Message.user_id).filter(
                Message.client_account_id == client_account_id,
                Message.template == TEMPLATE, Message.subject == subject,
                Message.delivery_status == "sent",
            )
        }

    now = utcnow()
    people = []
    for u in lapsed:
        eligible, reason = _why(
            u, info[u.id], last_paid.get(u.id), last_asked[u.id], u.id in sent_to, now
        )
        people.append(Person(u, eligible, reason))
    people.sort(key=lambda p: (not p.eligible, p.user.name.lower()))
    return people


def render(first_name: str, content: dict, unsub_url: str) -> str:
    return render_template(
        "emails/lapsed_offer.html", first_name=first_name, content=content, unsubscribe_url=unsub_url,
    )


def send_to(user: User, content: dict) -> Message:
    return send_email(
        user, user.email, content["subject"],
        render(user.name.split(" ")[0], content, unsubscribe_url(user)),
        TEMPLATE, user.client_account_id,
        transactional=False, implied_consent=True, unsubscribe_link=unsubscribe_url(user),
    )
