"""Pass 1 gate: ad-click URL with UTMs → child booking → card vaulted →
reminder jobs queued → attendance marked. Plus the unit-ish checks around it."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    AttendeeProfile,
    Booking,
    BookingStatus,
    ClassInstance,
    ClassType,
    EventOutbox,
    Lead,
    Message,
    PaymentMethodStatus,
    StripeCustomer,
    User,
    WaiverSignature,
    utcnow,
)
from app.services import stripe_service
from app.services.scheduling import upcoming_instances
from app.services.tzutil import today_local


def _first_instance(client_account, key="kids_7_10"):
    occ = upcoming_instances(client_account.id, segment_tag="youth")
    for o in occ:
        if o["instance"].class_type.key == key:
            return o["instance"]
    raise AssertionError("no seeded instance")


def _child_form(instance, birth_year=None, **overrides):
    year = birth_year or today_local().year - 8  # 8yo → fits 7–10
    data = {
        "attendee_kind": "child",
        "child_first_name": "Maya",
        "child_birth_year": str(year),
        "emergency_contact_name": "Sam Parent",
        "emergency_contact_phone": "604-555-0100",
        "guardian_name": "Sam Parent",
        "email": "sam.parent@example.com",
        "phone": "604-555-0123",
        "health_asthma": "y",
        "health_notes": "Peanut allergy — epipen in bag.",
        "consent_email": "y",
        "waiver_agree": "y",
        "signature": "Sam Parent",
    }
    data.update(overrides)
    return data


def _book_child(client, instance, **overrides):
    r = client.post(
        "/book/youth", data={"instance_id": str(instance.id)}, follow_redirects=False
    )
    assert r.status_code == 302, r.data
    r = client.post(
        "/book/youth/details", data=_child_form(instance, **overrides),
        follow_redirects=False,
    )
    return r


def test_e2e_ad_click_to_attendance(app, client, client_account, monkeypatch):
    # 1. Ad click with UTMs → landing sets the first-touch cookie
    r = client.get(
        "/youth?utm_source=meta&utm_medium=paid_social"
        "&utm_campaign=youth-launch&utm_content=youth-A&v=a"
    )
    assert r.status_code == 200
    assert any("b2f_attr" in c for c in r.headers.getlist("Set-Cookie"))

    # 2. Child booking through the funnel
    instance = _first_instance(client_account)
    r = _book_child(client, instance)
    assert r.status_code == 302, r.data

    guardian = db.session.query(User).filter_by(email="sam.parent@example.com").one()
    assert guardian.phone == "+16045550123"
    assert guardian.consent_email is True and guardian.consent_sms is False

    child = db.session.query(AttendeeProfile).filter_by(kind="child").one()
    assert child.first_name == "Maya"
    assert child.user_id == guardian.id
    assert child.health_json["answers"]["asthma"] is True
    assert "epipen" in child.health_json["notes"].lower()

    sig = db.session.query(WaiverSignature).one()
    assert sig.attendee_id == child.id
    assert sig.signed_by_user_id == guardian.id  # guardian signs for the minor

    booking = db.session.query(Booking).one()
    assert booking.attendee_id == child.id
    assert booking.kind == "trial"

    lead = db.session.query(Lead).one()
    assert lead.utm_campaign == "youth-launch"
    assert lead.utm_content == "youth-A"
    assert lead.status == "booked"

    events = {e.event_name for e in db.session.query(EventOutbox).all()}
    assert {"Lead", "Schedule"} <= events

    # Confirmation email + SMS to the GUARDIAN, logged for CASL
    msgs = (
        db.session.query(Message).filter_by(template="booking_confirmation").all()
    )
    assert {m.channel for m in msgs} == {"email", "sms"}
    assert all(m.user_id == guardian.id for m in msgs)
    # plus the staff new-sign-up alert
    assert db.session.query(Message).filter_by(template="admin_new_signup").count() == 1

    # 3. Card vault — stub Stripe (test mode, no network)
    class FakeSI:
        status = "succeeded"
        payment_method = "pm_test_123"

    r = client.get("/book/youth/card")
    # Master Plan §6 disclosure with confirmed $189 / 4-week billing cadence
    # + 5% GST (client 2026-08-21) — total must be stated for accuracy
    assert b"No charge today" in r.data
    assert b"$189 + 5% GST ($198.45 billed every 4 weeks)" in r.data
    customer = db.session.query(StripeCustomer).filter_by(user_id=guardian.id).one()
    customer.stripe_setup_intent_id = "seti_test_123"

    monkeypatch.setattr(stripe_service, "is_configured", lambda: True)

    class FakeStripe:
        class SetupIntent:
            @staticmethod
            def retrieve(_id):
                return FakeSI()

    monkeypatch.setattr(stripe_service, "stripe_client", lambda: FakeStripe)
    r = client.get("/book/complete")
    assert r.status_code == 302
    db.session.refresh(customer)
    assert customer.payment_method_status == PaymentMethodStatus.vaulted.value
    events = {e.event_name for e in db.session.query(EventOutbox).all()}
    assert "AddPaymentInfo" in events

    # 4. Reminder job queues the T-24h reminder (class is tomorrow)
    from app.tasks.jobs import send_due_reminders

    sent = send_due_reminders.apply().get()
    assert sent == 1
    db.session.refresh(booking)
    assert booking.reminder_24h_sent_at is not None
    reminder_msgs = db.session.query(Message).filter_by(template="reminder_24h").all()
    assert len(reminder_msgs) == 2  # email + sms, to the guardian

    # 5. Front desk marks attendance in the Today view
    staff_client = app.test_client()
    r = staff_client.post(
        "/ops/login", data={"email": "frontdesk@test.local", "password": "pw"}
    )
    assert r.status_code == 302
    # move class to today so it appears in Today view logic (attendance route
    # works regardless; assert the state transition)
    r = staff_client.post(
        f"/ops/bookings/{booking.id}/attendance", data={"action": "attended"}
    )
    assert r.status_code == 302
    db.session.refresh(booking)
    assert booking.status == BookingStatus.attended.value
    assert booking.checked_in_at is not None
    assert booking.attendance_marked_by == "frontdesk@test.local"


def test_age_bracket_validation(client, client_account):
    """A 14-year-old cannot be booked into Kids Boxing 7–10."""
    instance = _first_instance(client_account, "kids_7_10")
    r = _book_child(
        client, instance, birth_year=today_local().year - 14,
        email="other.parent@example.com",
    )
    # bounced back to class pick with an age error, nothing persisted
    assert r.status_code == 302
    assert "/book/youth" in r.headers["Location"]
    assert db.session.query(Booking).count() == 0
    r = client.get("/book/youth")
    assert b"is for ages 6" in r.data


def test_capacity_and_fullness(client, app, client_account):
    instance = _first_instance(client_account, "kids_7_10")  # capacity 2
    _book_child(client, instance)
    c2 = app.test_client()
    _book_child(
        client=c2, instance=instance,
        email="second.parent@example.com", guardian_name="Pat Second",
        signature="Pat Second",
    )
    assert db.session.query(Booking).count() == 2
    c3 = app.test_client()
    r = c3.post("/book/youth", data={"instance_id": str(instance.id)})
    assert r.status_code == 200
    assert b"full" in r.data.lower()


def test_confirmed_page_fires_deduped_pixel_events(app, client, client_account):
    """The confirmation page mirrors Lead + Schedule to the browser pixel
    with the SAME event_id as the CAPI outbox rows, so Meta dedups the
    pair — this is what ad campaigns optimize on."""
    from app.models import EventOutbox

    instance = _first_instance(client_account)
    _book_child(client, instance)
    app.config["META_PIXEL_ID"] = "123456789"
    try:
        r = client.get("/book/confirmed")
        html = r.data.decode()
        assert "fbq('track', 'Lead'" in html
        assert "fbq('track', 'Schedule'" in html
        outbox_ids = {
            e.event_id
            for e in db.session.query(EventOutbox).filter(
                EventOutbox.event_name.in_(("Lead", "Schedule"))
            )
        }
        assert sum(1 for i in outbox_ids if i in html) == 2  # exact id reuse
    finally:
        app.config["META_PIXEL_ID"] = ""


def test_honeypot_blocks_bots(client, client_account):
    instance = _first_instance(client_account)
    client.post("/book/youth", data={"instance_id": str(instance.id)})
    r = client.post(
        "/book/youth/details",
        data=_child_form(instance, website="http://spam.example"),
    )
    assert r.status_code == 200
    assert db.session.query(Booking).count() == 0


def test_signed_cancel_link(client, client_account):
    from app.services.signed_links import SALT_CANCEL_BOOKING, make_token

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    token = make_token(booking.id, SALT_CANCEL_BOOKING)
    r = client.get(f"/book/cancel/{token}")
    assert r.status_code == 200
    db.session.refresh(booking)
    assert booking.status == BookingStatus.cancelled.value
    # silent late-cancel flag matches the 12h policy window exactly
    hours_left = (
        booking.class_instance.starts_at_utc - utcnow()
    ).total_seconds() / 3600
    assert booking.late_cancel is (0 < hours_left < 12)
    # tampered token 404s
    assert client.get(f"/book/cancel/{token}x").status_code == 404


def test_api_classes_and_auth(client, client_account):
    r = client.get("/api/v1/classes?segment=youth&trials_only=1")
    data = r.get_json()
    assert r.status_code == 200
    assert len(data["classes"]) >= 2
    first = data["classes"][0]
    assert {"instance_id", "age_bracket", "cohort", "remaining", "starts_at_local"} <= set(first)
    cohorts = {c["cohort"] for c in data["classes"]}
    assert "Group A" in cohorts  # cohort label propagates template → instance → API

    r = client.post(
        "/api/v1/auth/token",
        json={"email": "frontdesk@test.local", "password": "pw"},
    )
    token = r.get_json()["token"]
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.get_json()["email"] == "frontdesk@test.local"

    r = client.get("/api/v1/me", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_ops_requires_staff(client):
    assert client.get("/ops/today").status_code == 302  # redirected to login
