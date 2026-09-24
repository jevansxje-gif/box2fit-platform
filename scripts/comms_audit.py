"""Everything we told one member, with timestamps, for a billing dispute.

Prints, for a guardian matched by name/email/child's name: waiver signatures
(what they agreed to and when), bookings and attendance, card status, the
subscription (created / activated / first charge), payments, and EVERY
message we sent (channel, template, recipient, delivery status, the text
preview incl. links) in time order, in local time. Read-only.

    .venv/bin/python -m scripts.comms_audit aisling
"""
import sys
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import (
    AttendeeProfile,
    Booking,
    ClassInstance,
    Message,
    Payment,
    StripeCustomer,
    Subscription,
    User,
    WaiverDocument,
    WaiverSignature,
)
from app.services.tzutil import fmt_local

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    raise SystemExit("usage: -m scripts.comms_audit <name-or-email>")
needle = f"%{args[0]}%"
app = create_app()


def L(dt):
    return fmt_local(dt, "%a %b %d %Y %I:%M %p") if dt else "—"


with app.app_context():
    ids = {a.user_id for a in db.session.query(AttendeeProfile).filter(AttendeeProfile.first_name.ilike(needle))}
    ids |= {u.id for u in db.session.query(User).filter((User.name.ilike(needle)) | (User.email.ilike(needle)))}
    if not ids:
        raise SystemExit(f"no member matching {args[0]!r}")
    for uid in sorted(ids):
        u = db.session.get(User, uid)
        print(f"\n===== {u.name} <{u.email}> {u.phone or ''} | account created {L(u.created_at)} | email consent={u.consent_email} sms consent={u.consent_sms}")
        atts = db.session.query(AttendeeProfile).filter_by(user_id=uid).all()
        for a in atts:
            print(f"  attendee #{a.id}: {a.first_name} {a.last_name or ''} ({a.kind})")
            for sig in db.session.query(WaiverSignature).filter_by(attendee_id=a.id).order_by(WaiverSignature.signed_at):
                doc = db.session.get(WaiverDocument, sig.document_id)
                print(f"    WAIVER SIGNED {L(sig.signed_at)} | doc kind={doc.kind} v{doc.version} | typed name: {sig.signature_name!r}")
            bks = (
                db.session.query(Booking)
                .join(ClassInstance, Booking.class_instance_id == ClassInstance.id)
                .filter(Booking.attendee_id == a.id)
                .order_by(ClassInstance.starts_at_utc)
                .all()
            )
            for b in bks:
                ci = b.class_instance
                extra = []
                if b.first_charge_on: extra.append(f"first_charge_on={b.first_charge_on}")
                if b.checked_in_at: extra.append(f"checked in {L(b.checked_in_at)} by {b.attendance_marked_by or '?'}")
                if b.cancelled_at: extra.append(f"cancelled {L(b.cancelled_at)}")
                print(f"    BOOKING #{b.id} {b.kind} | class {L(ci.starts_at_utc)} | status={b.status} | booked {L(b.booked_at)}" + (" | " + " | ".join(extra) if extra else ""))
        sc = db.session.query(StripeCustomer).filter_by(user_id=uid).one_or_none()
        if sc:  # noqa
            print(f"  CARD: status={sc.payment_method_status} customer={sc.stripe_customer_id} pm={sc.stripe_payment_method_id} (row created {L(sc.created_at)})")
        for s in db.session.query(Subscription).filter_by(user_id=uid).order_by(Subscription.id):
            print(f"  SUBSCRIPTION #{s.id} {s.status} ${s.mrr_cents/100:.0f}/4wk {s.cohort_label or ''} | first_charge_at {L(s.first_charge_at)} | activated_at {L(s.activated_at)}"
                  + (f" | cancel requested {L(s.cancel_requested_at)} effective {L(s.cancel_effective_at)} reason={s.cancel_reason}" if s.cancel_requested_at else "")
                  + f" | stripe {s.stripe_subscription_id}")
        try:
          pays = db.session.query(Payment).filter_by(user_id=uid).order_by(Payment.id).all()
        except Exception as e:  # noqa: BLE001
          db.session.rollback(); pays = []; print(f"  PAYMENTS: could not read ({type(e).__name__})")
        for p in pays:
            print(f"  PAYMENT {L(getattr(p, 'paid_at', None) or getattr(p, 'created_at', None))} | {p.status} ${p.amount_cents/100:.2f} (GST ${p.tax_cents/100:.2f}) | refunded ${ (p.refunded_cents or 0)/100:.2f} | {p.stripe_invoice_id} | {p.note or ''}")
        print("  MESSAGES (time order):")
        msgs = db.session.query(Message).filter_by(user_id=uid).order_by(Message.sent_at).all()
        first_charge = min([s.first_charge_at for s in db.session.query(Subscription).filter_by(user_id=uid) if s.first_charge_at] or [None])
        for m in msgs:
            flag = ""
            if m.template == "pre_charge_reminder" and first_charge:
                hrs = (first_charge - m.sent_at).total_seconds() / 3600
                flag = f"  <== pre-charge notice, {hrs:.0f} h before the scheduled first charge"
            body = " ".join((m.body_preview or "").split())
            print(f"    {L(m.sent_at)} | {m.channel:5} | {m.template:22} | to {m.recipient} | {m.delivery_status}{flag}")
            if m.subject:
                print(f"        subject: {m.subject}")
            if body:
                print(f"        text: {body[:420]}{'…' if len(body) > 420 else ''}")
        if not msgs:
            print("    (none recorded)")
    print()
