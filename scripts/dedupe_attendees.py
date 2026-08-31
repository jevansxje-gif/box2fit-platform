"""Merge duplicate child profiles created by repeated form submission
(prod 2026-08-29: one parent, four identical children in one minute — the
booking form now dedupes at source; this cleans up existing damage).

Duplicates = same guardian + same first name (case-insensitive) + same birth
year, kind=child. The earliest profile is kept; for each duplicate we delete
its bookings/waiver signatures/waitlist entries and their now-orphaned leads,
repoint messages and member notes at the keeper, then delete the profile.
A duplicate with a subscription or an attended booking is NEVER touched —
that's real history, flagged for a human instead.

    .venv/bin/python -m scripts.dedupe_attendees            # report only
    .venv/bin/python -m scripts.dedupe_attendees --delete   # actually merge
"""
import sys
from collections import defaultdict

from app import create_app
from app.extensions import db
from app.models import (
    AttendeeKind,
    AttendeeProfile,
    Booking,
    BookingStatus,
    Call,
    Lead,
    MemberNote,
    Message,
    Subscription,
    WaitlistEntry,
    WaiverSignature,
)

DELETE = "--delete" in sys.argv

app = create_app()

with app.app_context():
    groups = defaultdict(list)
    for a in (
        db.session.query(AttendeeProfile)
        .filter_by(kind=AttendeeKind.child.value)
        .order_by(AttendeeProfile.id)
        .all()
    ):
        groups[(a.user_id, (a.first_name or "").strip().lower(), a.birth_year)].append(a)

    merged = skipped = 0
    for (user_id, name, year), members in groups.items():
        if len(members) < 2:
            continue
        keeper, dups = members[0], members[1:]
        print(
            f"guardian {user_id} | child '{keeper.first_name}' b.{year}: "
            f"keeping attendee {keeper.id}, duplicates "
            f"{[d.id for d in dups]}"
        )
        for d in dups:
            has_sub = (
                db.session.query(Subscription)
                .filter_by(attendee_id=d.id)
                .count()
            )
            attended = (
                db.session.query(Booking)
                .filter_by(
                    attendee_id=d.id, status=BookingStatus.attended.value
                )
                .count()
            )
            if has_sub or attended:
                print(
                    f"  attendee {d.id}: SKIPPED (subscription or attended "
                    "history — needs a human)"
                )
                skipped += 1
                continue
            bookings = db.session.query(Booking).filter_by(attendee_id=d.id).all()
            lead_ids = {b.lead_id for b in bookings if b.lead_id}
            print(
                f"  attendee {d.id}: remove {len(bookings)} booking(s), "
                f"{len(lead_ids)} lead(s)"
            )
            if not DELETE:
                continue
            for b in bookings:
                db.session.delete(b)
            db.session.flush()
            for lid in lead_ids:
                still_used = (
                    db.session.query(Booking).filter_by(lead_id=lid).count()
                )
                if still_used:
                    continue
                db.session.query(Call).filter_by(matched_lead_id=lid).update(
                    {Call.matched_lead_id: None}, synchronize_session=False
                )
                lead = db.session.get(Lead, lid)
                if lead:
                    db.session.delete(lead)
            for w in (
                db.session.query(WaiverSignature).filter_by(attendee_id=d.id).all()
            ):
                db.session.delete(w)
            for wl in (
                db.session.query(WaitlistEntry).filter_by(attendee_id=d.id).all()
            ):
                db.session.delete(wl)
            db.session.query(Message).filter_by(attendee_id=d.id).update(
                {Message.attendee_id: keeper.id}, synchronize_session=False
            )
            db.session.query(MemberNote).filter_by(attendee_id=d.id).update(
                {MemberNote.attendee_id: keeper.id}, synchronize_session=False
            )
            db.session.delete(d)
            merged += 1
    if DELETE:
        db.session.commit()
        print(f"--- merged {merged} duplicate profile(s), skipped {skipped}")
    else:
        print("--- report only; rerun with --delete to merge")
