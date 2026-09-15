"""Backfill Payment rows (and activate subscriptions + record the agency
commission) from Stripe's paid invoices, for charges the invoice.paid
webhook missed. Idempotent — handle_invoice_paid skips invoices already
recorded, so this is safe to run repeatedly.

    .venv/bin/python -m scripts.backfill_payments          # report only
    .venv/bin/python -m scripts.backfill_payments --write  # apply
"""
import sys

from app import create_app
from app.extensions import db
from app.models import Payment, Subscription
from app.services import billing
from app.services.stripe_service import is_configured, stripe_client

WRITE = "--write" in sys.argv
app = create_app()

with app.app_context():
    if not is_configured():
        raise SystemExit("Stripe not configured.")
    st = stripe_client()
    subs = (
        db.session.query(Subscription)
        .filter(Subscription.stripe_subscription_id.isnot(None))
        .all()
    )
    before = db.session.query(Payment).count()
    processed = 0
    for s in subs:
        invoices = st.Invoice.list(
            subscription=s.stripe_subscription_id, status="paid", limit=100
        )
        for inv in invoices.auto_paging_iter():
            already = (
                db.session.query(Payment)
                .filter_by(stripe_invoice_id=inv.get("id"))
                .one_or_none()
            )
            if already:
                continue
            print(
                f"  sub {s.id} | invoice {inv.get('id')} | "
                f"${inv.get('amount_paid', 0) / 100} paid"
                + ("  -> recording" if WRITE else "  (report only)")
            )
            if WRITE:
                billing.handle_invoice_paid(inv)
                processed += 1
    if WRITE:
        db.session.commit()
    after = db.session.query(Payment).count()
    print("---")
    print(
        f"payments before: {before} | after: {after}"
        + (f" | recorded {processed}" if WRITE else " | rerun with --write to apply")
    )
