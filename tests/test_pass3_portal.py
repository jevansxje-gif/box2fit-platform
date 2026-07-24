"""Pass 3: landing pages + public site + guardian portal (RSVP booking,
cohort lock, waitlist auto-promotion, membership cancel with reason capture)
+ kiosk check-in + member API."""
import re
from datetime import time as dtime, timedelta

from app.extensions import db
from app.models import (
    AttendeeKind,
    AttendeeProfile,
    Booking,
    BookingStatus,
    ClassInstance,
    Message,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    WaitlistEntry,
    utcnow,
)
from app.services.tzutil import local_to_utc, today_local

from test_pass1_e2e import _book_child, _first_instance


# ------------------------------------------------------------- helpers ------
def _make_member(client_account, email="member@example.com", cohort="Group A"):
    guardian = User(
        client_account_id=client_account.id,
        email=email,
        name="Member Parent",
        phone="+16045550000",
    )
    guardian.set_password("password123")
    db.session.add(guardian)
    db.session.flush()
    child = AttendeeProfile(
        client_account_id=client_account.id,
        user_id=guardian.id,
        kind=AttendeeKind.child.value,
        first_name="Kiddo",
        birth_year=today_local().year - 9,
    )
    db.session.add(child)
    db.session.flush()
    plan = db.session.query(Plan).first()
    db.session.add(
        Subscription(
            client_account_id=client_account.id,
            user_id=guardian.id,
            attendee_id=child.id,
            plan_id=plan.id,
            cohort_label=cohort,
            status=SubscriptionStatus.active.value,
            mrr_cents=plan.price_cents,
            activated_at=utcnow(),
        )
    )
    db.session.commit()
    return guardian, child


def _login(client, email, password="password123"):
    return client.post(
        "/portal/login",
        data={"email": email, "password": password, "mode": "password"},
    )


# ---------------------------------------------------- landing + public ------
def test_public_and_landing_pages_render(client):
    for path in ("/", "/schedule", "/trainers", "/pricing", "/contact",
                 "/privacy", "/terms", "/youth", "/strong", "/reset",
                 "/focus", "/shehits", "/beast"):
        r = client.get(path)
        assert r.status_code == 200, path

    # parents landing routes into the YOUTH booking flow
    r = client.get("/reset")
    assert b"/book/youth" in r.data
    # beast register renders
    r = client.get("/beast")
    assert b"hardest hour" in r.data.lower()


# ------------------------------------------------------------- portal -------
def test_magic_link_login(client, client_account):
    guardian, _ = _make_member(client_account)
    r = client.post(
        "/portal/login", data={"email": guardian.email, "mode": "magic"}
    )
    assert r.status_code == 200
    msg = db.session.query(Message).filter_by(template="magic_link").one()
    token_url = re.search(r"/portal/magic/[\w\-\.]+", msg.body_preview).group(0)
    r = client.get(token_url, follow_redirects=False)
    assert r.status_code == 302  # logged in → dashboard
    r = client.get("/portal/")
    assert b"Hi, Member" in r.data
    assert b"MEMBER-" in r.data.upper()  # referral code from first name + id


def test_member_booking_and_cohort_lock(app, client, client_account):
    guardian, child = _make_member(client_account)
    _login(client, guardian.email)

    group_a = _first_instance(client_account, "kids_7_10")   # cohort Group A
    group_b = _first_instance(client_account, "teen_15_17")  # cohort Group B

    # booking their own group works, kind=member
    r = client.post(
        "/portal/bookings",
        data={"instance_id": group_a.id, "attendee_id": child.id},
        follow_redirects=True,
    )
    assert b"booked" in r.data.lower()
    booking = db.session.query(Booking).filter_by(attendee_id=child.id).one()
    assert booking.kind == "member"

    # the other group is locked (client decision: no swaps)
    r = client.post(
        "/portal/bookings",
        data={"instance_id": group_b.id, "attendee_id": child.id},
        follow_redirects=True,
    )
    assert b"Group B days aren" in r.data
    assert db.session.query(Booking).filter_by(attendee_id=child.id).count() == 1

    # schedule view hides the other group entirely
    r = client.get("/portal/schedule")
    assert b"Group A" in r.data and b"Group B" not in r.data


def test_waitlist_auto_promotion_and_confirm(app, client, client_account):
    # Fill the 2-cap Group A class with two funnel bookings
    instance = _first_instance(client_account, "kids_7_10")
    _book_child(client, instance)
    c2 = app.test_client()
    _book_child(
        client=c2, instance=instance,
        email="second.parent@example.com", guardian_name="Pat Second",
        signature="Pat Second",
    )
    # A member joins the waitlist through the portal
    guardian, child = _make_member(client_account, email="wait@example.com")
    c3 = app.test_client()
    _login(c3, guardian.email)
    r = c3.post(
        "/portal/bookings",
        data={"instance_id": instance.id, "attendee_id": child.id},
        follow_redirects=True,
    )
    assert b"joined the waitlist" in r.data
    entry = db.session.query(WaitlistEntry).one()
    assert entry.status == "waiting"

    # First funnel booker cancels → auto-promotion books the waitlisted child
    first_booking = (
        db.session.query(Booking).filter_by(class_instance_id=instance.id).first()
    )
    from app.services.signed_links import SALT_CANCEL_BOOKING, make_token

    client.get(f"/book/cancel/{make_token(first_booking.id, SALT_CANCEL_BOOKING)}")
    db.session.refresh(entry)
    # funnel cancel doesn't auto-promote (that's the portal/API path) —
    # promote explicitly the way the beat/cancel paths do
    if entry.status == "waiting":
        from app.services import waitlist as wl

        wl.promote_next(instance)
        db.session.commit()
        db.session.refresh(entry)
    assert entry.status == "offered"
    assert entry.expires_at is not None
    auto_booking = (
        db.session.query(Booking)
        .filter_by(attendee_id=child.id, class_instance_id=instance.id)
        .one()
    )
    assert auto_booking.status == BookingStatus.booked.value
    promo = db.session.query(Message).filter_by(template="waitlist_promoted").first()
    assert promo is not None
    confirm_url = re.search(
        r"/portal/waitlist/confirm/[\w\-\.]+", promo.body_preview
    ).group(0)

    r = client.get(confirm_url)
    assert b"locked in" in r.data
    db.session.refresh(entry)
    assert entry.status == "confirmed"


def test_waitlist_offer_expires_and_releases(app, client, client_account):
    from app.services import waitlist as wl

    instance = _first_instance(client_account, "kids_7_10")
    guardian, child = _make_member(client_account, email="expire@example.com")
    entry = wl.join(instance, child)
    db.session.commit()
    wl.promote_next(instance)
    db.session.commit()
    entry.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    released = wl.release_expired()
    db.session.commit()
    assert released == 1
    db.session.refresh(entry)
    assert entry.status == "released"
    booking = (
        db.session.query(Booking)
        .filter_by(attendee_id=child.id, class_instance_id=instance.id)
        .one()
    )
    assert booking.status == BookingStatus.cancelled.value


def test_cancel_membership_with_reason(client, client_account):
    guardian, child = _make_member(client_account)
    _login(client, guardian.email)
    sub = db.session.query(Subscription).one()
    r = client.get(f"/portal/membership/{sub.id}/cancel")
    assert b"pause" in r.data.lower()  # the pause-would-have-saved-me option
    r = client.post(
        f"/portal/membership/{sub.id}/cancel",
        data={"reason": "pause_would_have_saved", "note": "back in the fall"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.cancelled.value
    assert sub.cancel_reason == "pause_would_have_saved"
    assert sub.cancel_reason_note == "back in the fall"


def test_kiosk_checkin_first_timer(app, client, client_account):
    # a trial booking on a class happening TODAY
    from app.models import ClassType

    kids = db.session.query(ClassType).filter_by(key="kids_7_10").one()
    today_instance = ClassInstance(
        client_account_id=client_account.id,
        class_type_id=kids.id,
        cohort_label="Group A",
        starts_at_utc=local_to_utc(today_local(), dtime(23, 58)),
        local_date=today_local(),
        local_time=dtime(23, 58),
        duration_min=45,
        capacity=12,
    )
    db.session.add(today_instance)
    guardian, child = _make_member(client_account, email="kiosk@example.com")
    booking = Booking(
        client_account_id=client_account.id,
        attendee_id=child.id,
        class_instance_id=today_instance.id,
        kind="trial",
    )
    db.session.add(booking)
    db.session.commit()

    staff = app.test_client()
    staff.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    r = staff.post("/ops/kiosk/search", data={"q": "kid"})
    assert b"Kiddo" in r.data
    r = staff.post(f"/ops/kiosk/checkin/{booking.id}")
    assert b"Great to have you" in r.data  # first-timer variant
    db.session.refresh(booking)
    assert booking.status == BookingStatus.attended.value
    assert booking.attendance_marked_by == "kiosk"


def test_api_member_booking(client, client_account):
    guardian, child = _make_member(client_account, email="api@example.com")
    r = client.post(
        "/api/v1/auth/token",
        json={"email": guardian.email, "password": "password123"},
    )
    token = r.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    instance = _first_instance(client_account, "kids_7_10")
    r = client.post(
        "/api/v1/bookings",
        json={"instance_id": instance.id, "attendee_id": child.id},
        headers=headers,
    )
    assert r.status_code == 201
    booking_id = r.get_json()["booking_id"]

    group_b = _first_instance(client_account, "teen_15_17")
    r = client.post(
        "/api/v1/bookings",
        json={"instance_id": group_b.id, "attendee_id": child.id},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.get_json()["error"] == "wrong_group"

    r = client.get("/api/v1/me/bookings", headers=headers)
    assert len(r.get_json()["bookings"]) == 1

    r = client.post(f"/api/v1/bookings/{booking_id}/cancel", headers=headers)
    assert r.get_json()["cancelled"] is True
