"""Pass 2 gate: book → attend → activate → invoice.paid → member active →
past-due path → recovery. Plus refunds, cancel-before-charge, and the
agency-share math."""
import json

from app.extensions import db
from app.models import (
    AttendeeProfile,
    Booking,
    Message,
    Payment,
    PaymentMethodStatus,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services import billing
from app.services.scheduling import validate_bookable
from app.services.signed_links import SALT_CANCEL_BEFORE_CHARGE, make_token

from test_pass1_e2e import _book_child, _first_instance


def _vault_card(guardian: User) -> StripeCustomer:
    customer = StripeCustomer(
        user_id=guardian.id,
        stripe_customer_id=f"cus_test_{guardian.id}",
        payment_method_status=PaymentMethodStatus.vaulted.value,
        stripe_payment_method_id="pm_test_1",
    )
    db.session.add(customer)
    db.session.commit()
    return customer


def _webhook(client, event_type: str, obj: dict):
    return client.post(
        "/api/v1/webhooks/stripe",
        data=json.dumps({"type": event_type, "data": {"object": obj}}),
        content_type="application/json",
    )


def test_webhook_root_alias(client):
    """/webhooks/stripe (deploy-brief path) hits the same handler."""
    r = client.post(
        "/webhooks/stripe",
        data=json.dumps({"type": "noop.event", "data": {"object": {}}}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.get_json()["received"] is True


def _setup_member(app, client, client_account):
    """Funnel booking + vaulted card + attended class → ready to activate."""
    instance = _first_instance(client_account)
    _book_child(client, instance)
    guardian = db.session.query(User).filter_by(email="sam.parent@example.com").one()
    _vault_card(guardian)
    booking = db.session.query(Booking).one()

    staff = app.test_client()
    staff.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    staff.post(f"/ops/bookings/{booking.id}/attendance", data={"action": "attended"})
    return staff, booking, guardian


def test_e2e_money_lifecycle(app, client, client_account):
    staff, booking, guardian = _setup_member(app, client, client_account)

    # post-class 'loved it' email went to the guardian with the activate link
    post_class = db.session.query(Message).filter_by(template="post_class").one()
    assert "/activate/" in post_class.body_preview

    # 1. Front-desk activation → pending sub + pre-charge reminder (48h lead)
    r = staff.post(f"/ops/bookings/{booking.id}/activate")
    assert r.status_code == 302
    sub = db.session.query(Subscription).one()
    assert sub.status == SubscriptionStatus.pending.value
    assert sub.user_id == guardian.id
    assert sub.attendee_id == booking.attendee_id
    assert sub.cohort_label == "Group A"
    assert sub.mrr_cents == 18900
    assert sub.pre_charge_reminder_sent_at is not None
    reminder = db.session.query(Message).filter_by(template="pre_charge_reminder").all()
    assert {m.channel for m in reminder} == {"email", "sms"}
    assert "cancel-membership" in reminder[0].body_preview

    # double activation is rejected
    r = staff.post(f"/ops/bookings/{booking.id}/activate", follow_redirects=True)
    assert b"already has a membership" in r.data

    # 2. invoice.paid → payment row, 25% agency share, member active
    sub.stripe_subscription_id = "sub_test_1"
    db.session.commit()
    _webhook(client, "invoice.paid", {
        "id": "in_test_1", "subscription": "sub_test_1",
        "amount_paid": 18900, "currency": "cad", "charge": "ch_test_1",
    })
    db.session.refresh(sub)
    payment = db.session.query(Payment).one()
    assert payment.amount_cents == 18900
    assert payment.agency_share_cents == round(18900 * 0.25)  # 4725
    assert sub.status == SubscriptionStatus.active.value
    assert sub.activated_at is not None
    from app.models import Lead
    assert db.session.query(Lead).one().status == "activated"
    assert db.session.query(Message).filter_by(template="membership_welcome").count() == 1

    # redelivered webhook is idempotent
    _webhook(client, "invoice.paid", {
        "id": "in_test_1", "subscription": "sub_test_1",
        "amount_paid": 18900, "currency": "cad", "charge": "ch_test_1",
    })
    assert db.session.query(Payment).count() == 1

    # 3. payment fails → past_due, dunning sent, NEW bookings blocked
    _webhook(client, "invoice.payment_failed", {
        "id": "in_test_2", "subscription": "sub_test_1",
    })
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.past_due.value
    dunning = db.session.query(Message).filter_by(template="dunning").all()
    assert {m.channel for m in dunning} == {"email", "sms"}
    assert "/portal/card/" in dunning[0].body_preview

    attendee = db.session.get(AttendeeProfile, booking.attendee_id)
    with app.test_request_context():
        err = validate_bookable(_first_instance(client_account), attendee=attendee)
    assert err is not None and "card update" in err
    # existing booking is honored — still attended, untouched
    db.session.refresh(booking)
    assert booking.status == "attended"

    # card-update page loads from the signed dunning link (no login)
    import re
    token = re.search(r"/portal/card/([\w\-\.]+)", dunning[0].body_preview).group(1)
    r = client.get(f"/portal/card/{token}")
    assert r.status_code == 200
    assert b"payment didn" in r.data  # past-due banner

    # 4. recovery: next invoice.paid → active again, booking unblocked
    _webhook(client, "invoice.paid", {
        "id": "in_test_3", "subscription": "sub_test_1",
        "amount_paid": 18900, "currency": "cad", "charge": "ch_test_2",
    })
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.active.value
    assert db.session.query(Message).filter_by(template="payment_recovered").count() == 1
    with app.test_request_context():
        err = validate_bookable(_first_instance(client_account), attendee=attendee)
    assert err is None
    assert db.session.query(Payment).count() == 2

    # 5. refund reverses the agency share on the net
    _webhook(client, "charge.refunded", {
        "id": "ch_test_2", "invoice": "in_test_3", "amount_refunded": 18900,
    })
    refunded = db.session.query(Payment).filter_by(stripe_charge_id="ch_test_2").one()
    assert refunded.refunded_cents == 18900
    assert refunded.agency_share_cents == 0
    assert refunded.status == "refunded"

    # 6. subscription deleted on Stripe → churn recorded
    _webhook(client, "customer.subscription.deleted", {"id": "sub_test_1"})
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.cancelled.value
    assert sub.cancelled_at is not None


def test_activation_requires_vaulted_card(app, client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    staff = app.test_client()
    staff.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    staff.post(f"/ops/bookings/{booking.id}/attendance", data={"action": "attended"})
    r = staff.post(f"/ops/bookings/{booking.id}/activate", follow_redirects=True)
    assert b"No card on file" in r.data
    assert db.session.query(Subscription).count() == 0


def test_member_self_activation_link(app, client, client_account):
    staff, booking, guardian = _setup_member(app, client, client_account)
    import re
    post_class = db.session.query(Message).filter_by(template="post_class").one()
    token = re.search(r"/activate/([\w\-\.]+)", post_class.body_preview).group(1)

    r = client.get(f"/activate/{token}")
    assert r.status_code == 200
    assert b"$189" in r.data and b"every 4 weeks" in r.data

    r = client.post(f"/activate/{token}")
    assert b"crew" in r.data
    sub = db.session.query(Subscription).one()
    assert sub.status == SubscriptionStatus.pending.value


def test_cancel_before_charge(app, client, client_account):
    staff, booking, guardian = _setup_member(app, client, client_account)
    staff.post(f"/ops/bookings/{booking.id}/activate")
    sub = db.session.query(Subscription).one()

    token = make_token(sub.id, SALT_CANCEL_BEFORE_CHARGE)
    r = client.get(f"/cancel-membership/{token}")
    assert r.status_code == 200
    assert b"You won't be charged." in r.data
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.cancelled.value
    assert sub.cancel_reason == "cancelled_before_charge"
    assert sub.activated_at is None  # no charge ever happened
