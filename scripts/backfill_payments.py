"""Backfill Payment rows from Stripe for charges the webhooks missed —
both successful (records payment + commission, activates the sub) and
FAILED attempts (records a 'failed' row for the reconciliation ledger,
without re-sending dunning). Idempotent — safe to run repeatedly.

    .venv/bin/python -m scripts.backfill_payments          # report only
    .venv/bin/python -m scripts.backfill_payments --write  # apply
"""
import json
import sys

from app import create_app
from app.extensions import db
from app.models import ClientAccount, Payment, Subscription
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
    recorded_paid = recorded_failed = 0
    for s in subs:
        invoices = st.Invoice.list(
            subscription=s.stripe_subscription_id, limit=100
        )  # all statuses, so we see failures too
        for inv in invoices.auto_paging_iter():
            # Stripe objects don't expose .get() here — use plain dicts.
            invd = json.loads(str(inv))
            inv_id = invd.get("id")
            existing = (
                db.session.query(Payment)
                .filter_by(stripe_invoice_id=inv_id)
                .one_or_none()
            )
            status = invd.get("status")

            if status == "paid":
                amt = invd.get("amount_paid", 0)
                if amt <= 0:
                    continue  # $0 trial-start invoice — skip
                if existing and existing.status == "paid":
                    continue
                print(
                    f"  sub {s.id} | {inv_id} | ${amt / 100} PAID"
                    + ("  -> recording" if WRITE else "  (report only)")
                )
                if WRITE and billing.handle_invoice_paid(invd) is not None:
                    recorded_paid += 1

            elif status in ("open", "uncollectible") and (
                invd.get("attempt_count") or 0
            ) > 0:
                # A finalized invoice that has failed at least one charge.
                amt = invd.get("amount_due") or invd.get("total") or 0
                if amt <= 0 or existing:
                    continue
                print(
                    f"  sub {s.id} | {inv_id} | ${amt / 100} FAILED"
                    + ("  -> recording" if WRITE else "  (report only)")
                )
                if WRITE:
                    db.session.add(
                        Payment(
                            client_account_id=s.client_account_id,
                            user_id=s.user_id,
                            subscription_id=s.id,
                            stripe_invoice_id=inv_id,
                            stripe_charge_id=invd.get("charge"),
                            amount_cents=amt,
                            tax_cents=invd.get("tax") or 0,
                            currency=(invd.get("currency") or "cad").upper(),
                            status="failed",
                            agency_share_cents=0,
                            note="payment failed — recorded on backfill",
                        )
                    )
                    recorded_failed += 1
    if WRITE:
        db.session.commit()
    after = db.session.query(Payment).count()
    print("---")
    print(
        f"payments before: {before} | after: {after}"
        + (
            f" | recorded {recorded_paid} paid + {recorded_failed} failed"
            if WRITE
            else " | rerun with --write to apply"
        )
    )
