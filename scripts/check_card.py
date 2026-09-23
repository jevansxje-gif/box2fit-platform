"""Which card will Stripe charge for a member — and fix it if it's stale.

Looks a member up by a child's or guardian's name (or email), then asks
Stripe for the cards on file, which one the customer/subscription actually
defaults to, and when the next charge (or retry of a failed one) lands.
--fix makes the NEWEST card the charging card everywhere and immediately
retries any past-due invoice on it.

    .venv/bin/python -m scripts.check_card scarlett
    .venv/bin/python -m scripts.check_card scarlett --fix
"""
import sys
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import AttendeeProfile, StripeCustomer, Subscription, User
from app.services.stripe_service import (
    is_configured,
    make_default_card,
    stripe_client,
)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
FIX = "--fix" in sys.argv
if not args:
    raise SystemExit("usage: -m scripts.check_card <name-or-email> [--fix]")
needle = f"%{args[0]}%"
app = create_app()


def when(ts):
    if not ts:
        return "—"
    return datetime.utcfromtimestamp(int(ts)).strftime("%b %d %Y %H:%M UTC")


with app.app_context():
    if not is_configured():
        raise SystemExit("Stripe not configured.")
    st = stripe_client()
    st.api_version = "2019-09-09"  # top-level invoice fields, as the handlers expect
    guardian_ids = {
        a.user_id
        for a in db.session.query(AttendeeProfile)
        .filter(AttendeeProfile.first_name.ilike(needle))
        .all()
    }
    guardian_ids |= {
        u.id
        for u in db.session.query(User)
        .filter((User.name.ilike(needle)) | (User.email.ilike(needle)))
        .all()
    }
    if not guardian_ids:
        raise SystemExit(f"no member matching {args[0]!r}")

    for uid in sorted(guardian_ids):
        u = db.session.get(User, uid)
        kids = ", ".join(
            a.first_name
            for a in db.session.query(AttendeeProfile).filter_by(user_id=uid)
        )
        print(f"\n== {u.name} <{u.email}> {u.phone or ''} | attendees: {kids}")
        c = db.session.query(StripeCustomer).filter_by(user_id=uid).one_or_none()
        if not c or not c.stripe_customer_id:
            print("   no Stripe customer — never reached the card page")
            continue
        print(f"   local: status={c.payment_method_status} pm={c.stripe_payment_method_id}")
        cards = sorted(
            st.PaymentMethod.list(customer=c.stripe_customer_id, type="card").data,
            key=lambda pm: pm.created,
            reverse=True,
        )
        cus = st.Customer.retrieve(c.stripe_customer_id)
        cus_default = (cus.get("invoice_settings") or {}).get("default_payment_method")
        for pm in cards:
            tag = " <- customer default" if pm.id == cus_default else ""
            print(
                f"   card: {pm.card.brand} **** {pm.card.last4} "
                f"exp {pm.card.exp_month}/{pm.card.exp_year} "
                f"added {when(pm.created)} [{pm.id}]{tag}"
            )
        if not cards:
            print("   no cards on file")
        subs = (
            db.session.query(Subscription)
            .filter(
                Subscription.user_id == uid,
                Subscription.stripe_subscription_id.isnot(None),
            )
            .all()
        )
        for s in subs:
            ss = st.Subscription.retrieve(s.stripe_subscription_id)
            sub_pm = ss.get("default_payment_method") or cus_default
            pm_obj = next((p for p in cards if p.id == sub_pm), None)
            charging = f"**** {pm_obj.card.last4}" if pm_obj else (sub_pm or "NONE")
            print(
                f"   sub {s.id} [{s.stripe_subscription_id}] local={s.status} "
                f"stripe={ss.get('status')} | charges card: {charging} | "
                f"period ends {when(ss.get('current_period_end'))}"
            )
            open_invs = st.Invoice.list(
                subscription=s.stripe_subscription_id, status="open", limit=5
            ).data
            for inv in open_invs:
                print(
                    f"      OPEN invoice {inv.id} ${inv.amount_due / 100:.2f} "
                    f"attempts={inv.attempt_count} "
                    f"next retry: {when(inv.get('next_payment_attempt'))}"
                )
        if FIX and cards:
            newest = cards[0]
            res = make_default_card(c, newest.id)
            c.stripe_payment_method_id = newest.id
            db.session.commit()
            print(
                f"   FIXED -> charging card now **** {newest.card.last4}: "
                f"subs updated={res['subs']} retried={res['retried']} "
                f"paid now={res['paid']} errors={res['errors']}"
            )
    print()
