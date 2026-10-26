"""Call-first short form (owner 2026-10-26): page button -> short form ->
gym alert + Call list -> gym sends sign-up link -> pay/booking page filled
in -> Call list shows Paid / Booked."""
from app.extensions import db
from app.models import Enquiry, Message, SiteSetting, Subscription, utcnow

from test_pass4_ops import _admin


def _staff_alerts_on():
    SiteSetting.set("admin_notify_emails", "frankie@example.com")
    db.session.commit()


def test_adult_short_form_to_paid(app, client, client_account):
    _staff_alerts_on()
    # the challenge page button now opens the short form
    r = client.get("/challenge?utm_source=meta&utm_campaign=guided&utm_content=guided-g4")
    assert b'href="/start/challenge"' in r.data and b"Save my spot" in r.data

    r = client.get("/start/challenge")
    assert r.status_code == 200 and b"Date of birth" in r.data and b"5-Week Guided Boxing Challenge" in r.data
    assert b"No payment here" in r.data

    base = {"first_name": "Dana", "last_name": "Lee", "phone": "604-555-0199"}
    r = client.post("/start/challenge", data=base)
    assert b"date of birth" in r.data and db.session.query(Enquiry).count() == 0

    r = client.post("/start/challenge", data={**base, "dob": "1984-03-09", "times": ["evening", "late"], "sms_ok": "1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/start/challenge/thanks")
    e = db.session.query(Enquiry).one()
    assert e.offer == "challenge" and e.name == "Dana Lee" and e.email is None and e.sms_ok
    assert e.utm_content == "guided-g4" and e.best_time == "After work (5 to 7), Later evening (7 to 9)"
    alert = db.session.query(Message).filter_by(template="admin_new_enquiry").one()
    assert alert.recipient == "frankie@example.com"

    r = client.get("/start/challenge/thanks")
    assert b"Thanks, Dana. The gym will call you." in r.data
    assert b"fbq('track', 'Lead'" in r.data and b"/s/" in r.data

    # the gym's Call list
    staff = _admin(app)
    r = staff.get("/ops/calls")
    assert b"Dana Lee" in r.data and b"604-555-0199" in r.data and b"from A4" in r.data  # Meta's ad name
    r = staff.post(f"/ops/calls/{e.id}", data={"status": "no_answer", "notes": "left a voicemail"})
    db.session.refresh(e)
    assert e.status == "no_answer" and e.notes == "left a voicemail"

    r = staff.post(f"/ops/calls/{e.id}/send-link", data={"email": "dana@example.com"})
    db.session.refresh(e)
    assert e.status == "agreed" and e.link_sent_at is not None and e.email == "dana@example.com"
    assert db.session.query(Message).filter_by(template="enquiry_signup_link").count() == 1
    assert db.session.query(Message).filter_by(template="payment_link", channel="sms").count() == 1

    # they open the link on another phone: the join page is filled in
    from app.services import enquiry as enq_svc
    from app.services.signed_links import SALT_ENQUIRY, make_token

    phone = app.test_client()
    r = phone.get(f"/s/{make_token(e.id, SALT_ENQUIRY)}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/challenge/join")
    r = phone.get("/challenge/join")
    assert b'value="Dana Lee"' in r.data and b'value="dana@example.com"' in r.data and b'value="604-555-0199"' in r.data

    r = phone.post("/challenge/join", data={
        "name": "Dana Lee", "email": "dana@example.com", "phone": "604-555-0199",
        "waiver_agree": "1", "signature": "Dana Lee",
    })
    assert b"You're in, Dana" in r.data
    db.session.refresh(e)
    assert e.user_id is not None
    # attribution travelled with the link even without the ad cookie
    from app.models import Lead

    assert db.session.query(Lead).filter_by(user_id=e.user_id).one().utm_content == "guided-g4"

    sub = db.session.query(Subscription).filter_by(user_id=e.user_id).one()
    sub.activated_at = utcnow()
    db.session.commit()
    assert enq_svc.outcome(e) == "paid"
    r = staff.get("/ops/calls?show=all")
    assert b">Paid<" in r.data

    r = staff.get("/ops/marketing?c=guided")
    assert b"Call-first short form" in r.data and b"Dana Lee" in r.data


def test_kids_f2_gets_short_form_f5_keeps_booking(app, client, client_account):
    r = client.get("/kids?v=f5&utm_source=meta&utm_campaign=families&utm_content=families-f5")
    assert b"Book their free week" in r.data and b"/start/kids" not in r.data
    client = app.test_client()  # a different parent, who clicked F2
    r = client.get("/kids?utm_source=meta&utm_campaign=families&utm_content=families-f2")
    assert b'href="/start/kids">Save their free week' in r.data

    r = client.get("/start/kids")
    assert b"Child&#39;s date of birth" in r.data or b"Child's date of birth" in r.data
    r = client.post("/start/kids", data={"first_name": "Pat", "last_name": "Singh", "phone": "604 555 0100",
                                         "child_first_name": "Ravi", "child_dob": "2018-05-01"})
    assert r.status_code == 302
    e = db.session.query(Enquiry).one()
    assert e.offer == "kids" and e.child_first_name == "Ravi" and e.dob is None
    assert e.utm_content == "families-f2"

    # fast path from the thank-you page goes to the kids booking, filled in
    from app.services.signed_links import SALT_ENQUIRY, make_token

    r = client.get(f"/s/{make_token(e.id, SALT_ENQUIRY)}")
    assert r.headers["Location"].endswith("/book/kids")


def test_short_form_rejects_bad_input(app, client, client_account):
    r = client.post("/start/shehits", data={"first_name": "A", "last_name": "B", "phone": "123", "dob": "1990-01-01"})
    assert b"mobile number" in r.data
    r = client.post("/start/shehits", data={"first_name": "A", "last_name": "B", "phone": "6045550100", "dob": "2999-01-01"})
    assert b"date of birth" in r.data
    r = client.post("/start/shehits", data={"first_name": "A", "last_name": "B", "phone": "6045550100",
                                            "dob": "1990-01-01", "email": "nope"})
    assert b"email doesn" in r.data
    assert db.session.query(Enquiry).count() == 0
    assert client.get("/start/nonsense").status_code == 404
