"""Backfill Payment rows (and activate subscriptions + record the agency
commission) from Stripe's paid invoices, for charges the invoice.paid
webhook missed. Idempotent — handle_invoice_paid skips invoices already
recorded, so this is safe to run repeatedly.

    .venv/bin/python -m scripts.backfill_payments          # report only
    .venv/bin/python -m scripts.backfill_payments --write  # apply
"""
import json
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
    # Fetch invoices in the SAME API version the webhook/handler expect
    # (top-level subscription, tax, period_end, lines) — the account default
    # is newer and reshapes those, which makes the handler skip everything.
    st.api_version = "2019-09-09"
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
            # Stripe objects don't expose .get() here — use plain dicts.
            invd = json.loads(str(inv))
            already = (
                db.session.query(Payment)
                .filter_by(stripe_invoice_id=invd.get("id"))
                .one_or_none()
            )
            if already:
                continue
            amt = invd.get("amount_paid", 0) / 100
            tax = (invd.get("tax") or 0) / 100
            print(
                f"  sub {s.id} | invoice {invd.get('id')} | "
                f"${amt} paid (tax ${tax})"
                + ("  -> recording" if WRITE else "  (report only)")
            )
            if WRITE:
                p = billing.handle_invoice_paid(invd)
                if p is not None:
                    processed += 1
    if WRITE:
        db.session.commit()
    after = db.session.query(Payment).count()
    print("---")
    print(
        f"payments before: {before} | after: {after}"
        + (f" | recorded {processed}" if WRITE else " | rerun with --write to apply")
    )
