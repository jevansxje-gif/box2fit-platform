"""Email features: admin sign-up alerts, add-to-calendar, coach assignment."""
import re

from app.extensions import db
from app.models import Booking, Message, Trainer

from test_pass1_e2e import _book_child, _first_instance
from test_pass4_ops import _admin


def test_waiver_accordions_and_locked_consent(client, client_account):
    instance = _first_instance(client_account)
    client.post("/book/youth", data={"instance_id": str(instance.id)})
    r = client.get("/book/youth/details")
    # all three client legal sections render as accordions
    assert b"Waiver &amp; Release of Liability" in r.data
    assert b"Electronic Signatures" in r.data
    assert b"Membership Terms, Cancellation Policy" in r.data
    assert b"Frankie LaSasso" in r.data
    assert b"30-day cancellation notice" in r.data
    # consent checkbox starts locked until sections are opened (JS gate)
    assert b'data-waiver-consent="1"' in r.data
    assert b"disabled" in r.data
    # no stale "no contracts" claims anywhere on the page
    assert b"No contracts" not in r.data


def test_admin_alert_on_new_signup(client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    alert = db.session.query(Message).filter_by(template="admin_new_signup").one()
    assert alert.recipient == "staff-alerts@test.local"
    assert "Maya" in alert.subject
    assert "Sam Parent" in alert.body_preview
    assert "/ops/" in alert.body_preview  # link into the Today view


def test_confirmation_has_calendar_buttons_and_ics_works(client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    conf = (
        db.session.query(Message)
        .filter_by(template="booking_confirmation", channel="email")
        .one()
    )
    assert "calendar.google.com/calendar/render" in conf.body_preview
    ics_path = re.search(r"/calendar/[\w\-\.]+\.ics", conf.body_preview).group(0)

    r = client.get(ics_path)
    assert r.status_code == 200
    assert r.mimetype == "text/calendar"
    body = r.data.decode()
    assert "BEGIN:VEVENT" in body
    assert "Kids Boxing" in body
    assert "Maya" in body
    assert "1522 Finlay St" in body

    # tampered token 404s
    assert client.get(ics_path.replace(".ics", "x.ics")).status_code == 404


def test_reminder_checkin_button_and_confirm_flow(app, client, client_account):
    import re

    from app.tasks.jobs import send_due_reminders

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()

    sent = send_due_reminders.apply().get()
    assert sent == 1
    reminder = (
        db.session.query(Message)
        .filter_by(template="reminder_24h", channel="email")
        .one()
    )
    assert "Check In Now" in reminder.body_preview
    confirm_url = re.search(r"/confirm/[\w\-\.]+", reminder.body_preview).group(0)

    r = client.get(confirm_url)
    assert b"checked in for class" in r.data
    db.session.refresh(booking)
    assert booking.confirmed_at is not None
    assert booking.status == "booked"  # confirmed is NOT attended

    # roster shows the confirmed chip to staff
    from app.services.tzutil import local_to_utc, today_local
    from datetime import time as dtime

    inst = booking.class_instance
    inst.local_date = today_local()
    inst.local_time = dtime(23, 55)
    inst.starts_at_utc = local_to_utc(today_local(), dtime(23, 55))
    db.session.commit()
    staff = _admin(app)
    assert "confirmed ✓".encode() in staff.get("/ops/today").data

    # tampered token 404s
    assert client.get(confirm_url + "x").status_code == 404


def test_alert_recipient_list_managed_in_ops(app, client, client_account):
    from app.models import SiteSetting

    staff = _admin(app)
    # add two recipients via the UI
    staff.post(
        "/ops/announcements",
        data={"action": "add_alert_email", "alert_email": "owner@example.com"},
    )
    staff.post(
        "/ops/announcements",
        data={"action": "add_alert_email", "alert_email": "second@example.com"},
    )
    r = staff.get("/ops/announcements")
    assert b"owner@example.com" in r.data and b"second@example.com" in r.data

    # a sign-up alerts EVERY listed mailbox — including the pre-existing env
    # recipient, which the first edit folds into the managed list
    instance = _first_instance(client_account)
    _book_child(client, instance)
    alerts = db.session.query(Message).filter_by(template="admin_new_signup").all()
    assert sorted(a.recipient for a in alerts) == [
        "owner@example.com",
        "second@example.com",
        "staff-alerts@test.local",
    ]

    # remove one
    staff.post(
        "/ops/announcements",
        data={"action": "remove_alert_email", "alert_email": "second@example.com"},
    )
    assert SiteSetting.get("admin_notify_emails") == (
        "staff-alerts@test.local,owner@example.com"
    )


def test_coach_assignment_email_on_bulk_and_day_swap(app, client, client_account):
    staff = _admin(app)
    coach = Trainer(
        client_account_id=client_account.id,
        name="Coach Mailme",
        email="coach@test.local",
        active=True,
    )
    db.session.add(coach)
    from app.models import ClassInstance, ScheduleTemplate

    for tpl in db.session.query(ScheduleTemplate).all():
        tpl.trainer_id = None
    for inst in db.session.query(ClassInstance).all():
        inst.trainer_id = None
    db.session.commit()

    # bulk assign → one summary email
    staff.post(
        "/ops/schedule-builder",
        data={
            "action": "bulk_assign",
            "bulk_trainer_id": str(coach.id),
            "scope": "unassigned",
        },
    )
    notices = db.session.query(Message).filter_by(template="coach_assignment").all()
    assert len(notices) == 1
    assert notices[0].recipient == "coach@test.local"
    assert "every" in notices[0].body_preview

    # per-day swap → a "this day only" email
    instance = _first_instance(client_account, "teen_15_17")
    staff.post(
        f"/ops/instances/{instance.id}/coach", data={"trainer_id": str(coach.id)}
    )
    notices = db.session.query(Message).filter_by(template="coach_assignment").all()
    assert len(notices) == 2
    assert "this day only" in notices[-1].body_preview
