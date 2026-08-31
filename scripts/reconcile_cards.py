"""Reconcile card-on-file status against Stripe (the source of truth).

The local `payment_method_status` is stamped on the browser's return from the
Stripe card page; people who close the tab right after saving never trigger
it, and the live webhook initially missed setup_intent.succeeded. This asks
Stripe directly for each customer's saved cards, prints the masked details,
and heals any stale local rows. Safe to run any time:

    .venv/bin/python -m scripts.reconcile_cards
"""
from app import create_app
from app.extensions import db
from app.models import PaymentMethodStatus, StripeCustomer, User
from app.services.stripe_service import is_configured, stripe_client

app = create_app()

with app.app_context():
    if not is_configured():
        raise SystemExit("Stripe is not configured; nothing to reconcile.")
    stripe = stripe_client()
    rows = db.session.query(StripeCustomer).all()
    with_card = healed = 0
    for c in rows:
        user = db.session.get(User, c.user_id)
        label = f"{user.name} | {user.email}"
        if not c.stripe_customer_id:
            print(f"{label} | NO CARD (never reached the card page)")
            continue
        pms = stripe.PaymentMethod.list(
            customer=c.stripe_customer_id, type="card"
        ).data
        if not pms:
            print(f"{label} | NO CARD (opened the card page, didn't finish)")
            continue
        card = pms[0].card
        print(f"{label} | {card.brand} **** {card.last4}")
        with_card += 1
        if c.payment_method_status != PaymentMethodStatus.vaulted.value:
            c.payment_method_status = PaymentMethodStatus.vaulted.value
            c.stripe_payment_method_id = pms[0].id
            healed += 1
    db.session.commit()
    print("---")
    print(
        f"cards on file: {with_card} of {len(rows)} who reached the card step"
        + (f" | fixed {healed} stale local record(s)" if healed else "")
    )
