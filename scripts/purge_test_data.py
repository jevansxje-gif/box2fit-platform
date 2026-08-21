"""Purge test accounts from a live database — list first, delete only what
was explicitly named, confirm interactively.

Usage (on the server):
    .venv/bin/python -m scripts.purge_test_data
        -> numbered list of every non-admin user with their footprint

    .venv/bin/python -m scripts.purge_test_data --delete 3,5,12
        -> shows exactly what those user ids own, asks for 'yes', deletes

Deletes, per user: bookings, waitlist entries, waiver signatures, member
notes, messages, payments, subscriptions, attendee profiles, Stripe
customer link, leads sharing the email, and finally the user. Trainer rows
linked to a deleted login are unlinked, never deleted (remove those on the
Trainers page). gym_admin / agency_admin accounts are refused outright.
Stripe-side objects are NOT touched — test-mode data can be left to expire,
and live-mode should never be purged this way.
"""
import argparse
import sys

from app import create_app
from app.extensions import db
from app.models import (
    AttendeeProfile,
    Booking,
    Lead,
    MemberNote,
    Message,
    Payment,
    Role,
    StripeCustomer,
    Subscription,
    Trainer,
    User,
    WaitlistEntry,
    WaiverSignature,
)

PROTECTED_ROLES = {Role.gym_admin.value, Role.agency_admin.value}


def _footprint(user: User) -> dict:
    attendee_ids = [
        a.id
        for a in db.session.query(AttendeeProfile).filter_by(user_id=user.id)
    ]
    count = lambda q: q.count()  # noqa: E731
    return {
        "attendees": len(attendee_ids),
        "bookings": count(
            db.session.query(Booking).filter(
                Booking.attendee_id.in_(attendee_ids or [0])
            )
        ),
        "subscriptions": count(
            db.session.query(Subscription).filter_by(user_id=user.id)
        ),
        "payments": count(db.session.query(Payment).filter_by(user_id=user.id)),
        "messages": count(db.session.query(Message).filter_by(user_id=user.id)),
        "stripe": count(
            db.session.query(StripeCustomer).filter_by(user_id=user.id)
        ),
    }


def list_users() -> None:
    users = (
        db.session.query(User)
        .filter(User.role.notin_(PROTECTED_ROLES))
        .order_by(User.id)
        .all()
    )
    if not users:
        print("no non-admin users found")
        return
    print(f"{'id':>4}  {'role':<10} {'created':<12} {'email':<38} footprint")
    for u in users:
        f = _footprint(u)
        fp = ", ".join(f"{v} {k}" for k, v in f.items() if v)
        created = u.created_at.strftime("%Y-%m-%d") if u.created_at else "?"
        print(f"{u.id:>4}  {u.role:<10} {created:<12} {u.email:<38} {fp or '-'}")
    print(
        "\nre-run with --delete <ids> (comma-separated) to remove specific "
        "accounts.\nNOTE: admin roles are never listed or deletable here."
    )


def delete_users(ids: list[int]) -> None:
    users = db.session.query(User).filter(User.id.in_(ids)).all()
    found = {u.id for u in users}
    for missing in set(ids) - found:
        print(f"user {missing}: not found — skipped")
    users = [u for u in users if u.role not in PROTECTED_ROLES]
    refused = found - {u.id for u in users}
    for r in refused:
        print(f"user {r}: admin role — REFUSED")
    if not users:
        print("nothing to delete")
        return

    print("\nThis will permanently delete:")
    for u in users:
        f = _footprint(u)
        fp = ", ".join(f"{v} {k}" for k, v in f.items() if v)
        print(f"  [{u.id}] {u.email} ({u.role}) — {fp or 'no records'}")
    answer = input("\ntype 'yes' to delete: ").strip().lower()
    if answer != "yes":
        print("aborted — nothing deleted")
        return

    for u in users:
        attendee_ids = [
            a.id
            for a in db.session.query(AttendeeProfile).filter_by(user_id=u.id)
        ] or [0]
        db.session.query(Booking).filter(
            Booking.attendee_id.in_(attendee_ids)
        ).delete(synchronize_session=False)
        db.session.query(WaitlistEntry).filter(
            WaitlistEntry.attendee_id.in_(attendee_ids)
        ).delete(synchronize_session=False)
        db.session.query(WaiverSignature).filter(
            (WaiverSignature.attendee_id.in_(attendee_ids))
            | (WaiverSignature.signed_by_user_id == u.id)
        ).delete(synchronize_session=False)
        db.session.query(MemberNote).filter(
            (MemberNote.user_id == u.id) | (MemberNote.author_user_id == u.id)
        ).delete(synchronize_session=False)
        db.session.query(Message).filter(
            (Message.user_id == u.id)
            | (Message.attendee_id.in_(attendee_ids))
        ).delete(synchronize_session=False)
        db.session.query(Payment).filter_by(user_id=u.id).delete(
            synchronize_session=False
        )
        db.session.query(Subscription).filter_by(user_id=u.id).delete(
            synchronize_session=False
        )
        db.session.query(StripeCustomer).filter_by(user_id=u.id).delete(
            synchronize_session=False
        )
        db.session.query(Lead).filter(
            (Lead.user_id == u.id) | (Lead.email == u.email)
        ).delete(synchronize_session=False)
        db.session.query(AttendeeProfile).filter_by(user_id=u.id).delete(
            synchronize_session=False
        )
        for t in db.session.query(Trainer).filter_by(user_id=u.id):
            t.user_id = None
            print(f"  trainer '{t.name}' unlinked from deleted login")
        db.session.delete(u)
        print(f"  deleted [{u.id}] {u.email}")
    db.session.commit()
    print("done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", help="comma-separated user ids")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        if args.delete:
            try:
                ids = [int(x) for x in args.delete.split(",") if x.strip()]
            except ValueError:
                sys.exit("--delete takes comma-separated integer ids")
            delete_users(ids)
        else:
            list_users()
