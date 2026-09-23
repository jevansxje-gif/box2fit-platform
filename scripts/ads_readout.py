"""What the ads actually produced on OUR side — the funnel truth for the
market study. Per campaign/creative (utm_campaign / utm_content): leads,
bookings, attended, cards saved, memberships started, and revenue, since a
given date. Read-only.

    .venv/bin/python -m scripts.ads_readout            # since 2026-08-31
    .venv/bin/python -m scripts.ads_readout 2026-09-01
"""
import sys
from collections import defaultdict
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import (
    AttendeeProfile,
    Booking,
    BookingStatus,
    Lead,
    Payment,
    StripeCustomer,
    Subscription,
    User,
)

since = datetime.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime(2026, 8, 31)
app = create_app()

with app.app_context():
    leads = db.session.query(Lead).filter(Lead.created_at >= since).all()
    rows = defaultdict(lambda: defaultdict(int))
    for l in leads:
        key = (l.utm_source or l.landing_variant or "direct", l.utm_campaign or "-", l.utm_content or "-")
        r = rows[key]
        r["leads"] += 1
        r["status_" + (l.status or "?")] += 1
        bookings = db.session.query(Booking).filter_by(lead_id=l.id).all()
        if bookings:
            r["booked"] += 1
        if any(b.status == BookingStatus.attended.value for b in bookings):
            r["attended"] += 1
        if any(b.status == BookingStatus.no_show.value for b in bookings):
            r["no_show"] += 1
        uid = l.user_id if hasattr(l, "user_id") else None
        if uid is None and bookings:
            att = db.session.get(AttendeeProfile, bookings[0].attendee_id)
            uid = att.user_id if att else None
        if uid:
            sc = db.session.query(StripeCustomer).filter_by(user_id=uid).one_or_none()
            if sc and sc.payment_method_status == "vaulted":
                r["card"] += 1
            subs = db.session.query(Subscription).filter(Subscription.user_id == uid).all()
            if any(s.status in ("pending", "active", "past_due") for s in subs):
                r["member"] += 1
            paid = (
                db.session.query(Payment)
                .filter(Payment.user_id == uid, Payment.status == "paid")
                .all()
            )
            r["revenue_cents"] += sum(p.amount_cents - (p.refunded_cents or 0) for p in paid)

    print(f"\nsince {since.date()} | {len(leads)} leads\n")
    hdr = f"{'source':8} {'campaign':10} {'content':12} {'leads':>5} {'booked':>6} {'attend':>6} {'noshow':>6} {'card':>4} {'member':>6} {'revenue':>9}"
    print(hdr)
    print("-" * len(hdr))
    for key in sorted(rows, key=lambda k: -rows[k]["leads"]):
        r = rows[key]
        print(
            f"{key[0][:8]:8} {key[1][:10]:10} {key[2][:12]:12} {r['leads']:>5} {r['booked']:>6} "
            f"{r['attended']:>6} {r['no_show']:>6} {r['card']:>4} {r['member']:>6} "
            f"${r['revenue_cents'] / 100:>8.2f}"
        )
    tot = defaultdict(int)
    for r in rows.values():
        for k, v in r.items():
            tot[k] += v
    print("-" * len(hdr))
    print(
        f"{'TOTAL':32} {tot['leads']:>5} {tot['booked']:>6} {tot['attended']:>6} "
        f"{tot['no_show']:>6} {tot['card']:>4} {tot['member']:>6} ${tot['revenue_cents'] / 100:>8.2f}"
    )
    statuses = {k[7:]: v for k, v in tot.items() if k.startswith("status_")}
    print(f"\nlead statuses: {dict(sorted(statuses.items()))}\n")
