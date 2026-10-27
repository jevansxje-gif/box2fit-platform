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
    assert b"No card details needed." in r.data

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


def test_kids_f2_waiver_first_then_card_after_class(app, client, client_account):
    from datetime import timedelta

    from app.models import AttendeeProfile, WaiverSignature
    from app.services import enquiry as enq_svc
    from app.services.tzutil import today_local

    _staff_alerts_on()
    r = client.get("/kids?v=f5&utm_source=meta&utm_campaign=families&utm_content=families-f5")
    assert b"Book their free week" in r.data and b"/start/kids" not in r.data
    client = app.test_client()  # a different parent, who clicked F2
    r = client.get("/kids?utm_source=meta&utm_campaign=families&utm_content=families-f2")
    assert b'href="/start/kids">Save their free week' in r.data and b"No card details needed." in r.data

    r = client.get("/start/kids")
    assert b"No card details needed." in r.data and b"waiver-agree" in r.data
    assert b"Waiver &amp; Release" in r.data and b"Membership Terms" not in r.data  # terms come with the card link
    base = {"first_name": "Pat", "last_name": "Singh", "phone": "604 555 0100", "email": "pat@example.com",
            "child_first_name": "Ravi", "child_dob": "2018-05-01"}
    r = client.post("/start/kids", data=base)
    assert b"tick the box" in r.data and db.session.query(Enquiry).count() == 0
    r = client.post("/start/kids", data={**base, "waiver_agree": "1", "signature": "Pat Singh"})
    assert r.status_code == 302
    e = db.session.query(Enquiry).one()
    assert e.offer == "kids" and e.child_first_name == "Ravi" and e.utm_content == "families-f2"
    child = db.session.get(AttendeeProfile, e.attendee_id)
    assert child.first_name == "Ravi" and child.kind == "child" and child.user_id == e.user_id
    sig = db.session.query(WaiverSignature).filter_by(attendee_id=child.id).one()
    from app.models import WaiverDocument

    assert db.session.get(WaiverDocument, sig.document_id).kind == "minor_rel"  # release only
    r = client.get("/start/kids/thanks")
    assert b"waiver is signed" in r.data and b"Already decided" not in r.data
    assert enq_svc.stage(e) == "to_call"

    staff = _admin(app)
    assert b"Pat Singh" in staff.get("/ops/calls").data
    # booked on the phone for a class still to come
    future = today_local() + timedelta(days=2)
    staff.post(f"/ops/calls/{e.id}/book", data={"booked_for": future.isoformat()})
    db.session.refresh(e)
    assert enq_svc.stage(e) == "booked" and b"Pat Singh" in staff.get("/ops/calls?show=booked").data
    # after the class it moves to To pay
    e.booked_for = today_local() - timedelta(days=1)
    db.session.commit()
    assert enq_svc.stage(e) == "to_pay" and b"Pat Singh" in staff.get("/ops/calls?show=to_pay").data

    staff.post(f"/ops/calls/{e.id}/send-card", data={"show": "to_pay"})
    db.session.refresh(e)
    assert e.card_sent_at is not None
    msg = db.session.query(Message).filter_by(template="enquiry_card_link").one()
    assert msg.recipient == "pat@example.com"
    link = enq_svc.card_link(e)
    path = link.split("health.box2fit.com")[-1].split("localhost")[-1]
    path = path[path.index("/p/"):]
    r = client.get(path)  # terms first, then the card
    assert b"Membership Terms" in r.data and b"Keep Ravi" in r.data and b"Today: nothing." in r.data
    r = client.post(path, data={"signature": "Pat Singh"})
    assert b"tick the box" in r.data
    r = client.post(path, data={"signature": "Pat Singh", "terms_agree": "1"})
    assert r.status_code == 302 and "/membership/start/" in r.headers["Location"]
    full = db.session.query(WaiverSignature).filter_by(attendee_id=child.id).order_by(WaiverSignature.id.desc()).first()
    assert db.session.get(WaiverDocument, full.document_id).kind == "minor"  # release + terms

    # card added -> membership pending -> Paid
    from app.models import Subscription as Sub
    from app.services.billing import default_plan

    plan = default_plan(client_account.id)
    db.session.add(Sub(client_account_id=client_account.id, user_id=e.user_id, attendee_id=child.id,
                       plan_id=plan.id, status="pending", mrr_cents=plan.price_cents))
    db.session.commit()
    assert enq_svc.stage(e) == "paid"
    assert b"Pat Singh" in staff.get("/ops/calls?show=paid").data


def test_bootcamp_page_form_book_card_paid(app, client, client_account):
    from datetime import timedelta

    from app.models import WaiverSignature
    from app.services import challenge as ch
    from app.services import enquiry as enq_svc
    from app.services.signed_links import SALT_ENQUIRY_PAY, make_token
    from app.services.tzutil import today_local

    _staff_alerts_on()
    r = client.get("/bootcamp-7pm?utm_source=meta&utm_campaign=bootcamp&utm_content=bootcamp-b1")
    assert r.status_code == 200 and b"No card details needed." in r.data and b'href="/start/bootcamp"' in r.data
    assert b"7 pm" in r.data and b"$249" not in r.data and b"November 2" in r.data  # no price until the call

    data = {"first_name": "Jo", "last_name": "Park", "phone": "604-555-0111", "email": "jo@example.com",
            "dob": "1990-02-02", "waiver_agree": "1", "signature": "Jo Park"}
    r = client.post("/start/bootcamp", data={**data, "email": ""})
    assert b"enter your email" in r.data
    r = client.post("/start/bootcamp", data=data)
    assert r.status_code == 302
    e = db.session.query(Enquiry).one()
    assert e.offer == "bootcamp" and e.user_id and e.attendee_id and e.utm_content == "bootcamp-b1"
    assert db.session.query(WaiverSignature).filter_by(attendee_id=e.attendee_id).count() == 1
    assert db.session.query(Message).filter_by(template="admin_new_enquiry").count() == 1

    staff = _admin(app)
    staff.post(f"/ops/calls/{e.id}/book", data={"booked_for": today_local().isoformat()})
    db.session.refresh(e)
    assert enq_svc.stage(e) == "to_pay"  # class is today: card link can go out after it
    staff.post(f"/ops/calls/{e.id}/send-card")
    db.session.refresh(e)
    assert e.card_sent_at and "/p/" in enq_svc.card_link(e)

    # the card link: terms first (no Stripe in tests: the spot is held locally)
    r = client.get(f"/p/{make_token(e.id, SALT_ENQUIRY_PAY)}")
    assert b"Membership Terms" in r.data and b"What you pay" in r.data and b"Keep your spot, Jo." in r.data
    r = client.post(f"/p/{make_token(e.id, SALT_ENQUIRY_PAY)}", data={"signature": "Jo Park", "terms_agree": "1"})
    assert r.status_code == 200 and b"You're in" in r.data
    sub = db.session.query(Subscription).filter_by(user_id=e.user_id).one()
    assert sub.challenge_key == ch.BOOTCAMP["key"]
    sub.activated_at = utcnow()
    db.session.commit()
    assert enq_svc.stage(e) == "paid"
    r = staff.get("/ops/marketing?c=bootcamp")
    assert b"Boot camp 7 pm" in r.data and b"Jo Park" in r.data
    assert client.get(f"/p/{make_token(999, SALT_ENQUIRY_PAY)}").status_code == 404


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
