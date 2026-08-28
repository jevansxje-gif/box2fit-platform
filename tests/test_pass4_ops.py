"""Pass 4: schedule builder + cancel-class notifications, substitutions,
member directory + notes + flags, reports + CSV, announcements, reviews,
outbox dispatch, call matching — and the FINAL full-platform E2E."""
import json
import re
from datetime import timedelta

from app.extensions import db
from app.models import (
    Announcement,
    Booking,
    BookingStatus,
    Call,
    ClassInstance,
    EventOutbox,
    InstanceStatus,
    Lead,
    MemberNote,
    Message,
    Payment,
    Review,
    Role,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
    WaitlistEntry,
    utcnow,
)
from app.services.dispatch import drain, hash_user_data

from test_pass1_e2e import _book_child, _first_instance
from test_pass2_money import _vault_card, _webhook
from test_pass3_portal import _login, _make_member


def _admin(app):
    """A gym_admin staff client (reports are admin-only)."""
    u = User(
        client_account_id=1, email="admin@test.local", name="Gym Admin",
        role=Role.gym_admin.value,
    )
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    c.post("/ops/login", data={"email": "admin@test.local", "password": "pw"})
    return c


# ----------------------------------------------------- schedule + trainers ---
def test_schedule_builder_and_cancel_class(app, client, client_account):
    staff = _admin(app)
    r = staff.get("/ops/schedule-builder")
    assert r.status_code == 200

    # add a template with a trainer conflict → rejected
    from app.models import ClassType, ScheduleTemplate, Trainer

    kids = db.session.query(ClassType).filter_by(key="kids_7_10").one()
    trainer = db.session.query(Trainer).one()
    existing = db.session.query(ScheduleTemplate).first()
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "add_template",
            "class_type_id": kids.id,
            "weekday": existing.weekday,
            "start_time": existing.start_time_local.strftime("%H:%M"),
            "trainer_id": trainer.id,
        },
        follow_redirects=True,
    )
    assert b"coach already leads" in r.data
    assert b"Nothing added" in r.data

    # cancel a class with a booking → member notified, waitlist dissolved
    instance = _first_instance(client_account)
    _book_child(client, instance)
    guardian, child = _make_member(client_account, email="wl@example.com")
    from app.services import waitlist as wl

    wl.join(instance, child)
    db.session.commit()

    r = staff.post(
        f"/ops/instances/{instance.id}",
        data={"action": "cancel_class"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    db.session.refresh(instance)
    assert instance.status == InstanceStatus.cancelled.value
    booking = db.session.query(Booking).filter_by(class_instance_id=instance.id).one()
    assert booking.status == BookingStatus.cancelled.value
    cancel_msgs = db.session.query(Message).filter_by(template="class_cancelled").all()
    assert len(cancel_msgs) == 2  # email + sms
    assert db.session.query(WaitlistEntry).one().status == "released"


def test_deactivate_template_removes_future_classes(app, client, client_account):
    from app.models import ScheduleTemplate
    from app.services.scheduling import upcoming_instances

    staff = _admin(app)
    instance = _first_instance(client_account, "kids_7_10")
    _book_child(client, instance)  # one booked occurrence + one empty next week
    tpl = db.session.get(ScheduleTemplate, instance.template_id)

    r = staff.post(
        "/ops/schedule-builder",
        data={"action": "toggle_template", "template_id": tpl.id},
        follow_redirects=True,
    )
    assert b"deactivated" in r.data

    # gone from every schedule surface
    with app.test_request_context():
        occ = upcoming_instances(client_account.id, segment_tag="youth")
    assert all(o["instance"].class_type.key != "kids_7_10" for o in occ)
    assert b"Kids Boxing" not in staff.get("/ops/schedule").data  # live tab
    assert b"Kids Boxing" not in client.get("/book/youth").data   # funnel picker

    # the booked occurrence was cancelled WITH member notification
    db.session.refresh(instance)
    assert instance.status == InstanceStatus.cancelled.value
    assert db.session.query(Message).filter_by(template="class_cancelled").count() >= 1

    # reactivating puts the classes back
    staff.post(
        "/ops/schedule-builder",
        data={"action": "toggle_template", "template_id": tpl.id},
    )
    with app.test_request_context():
        occ = upcoming_instances(client_account.id, segment_tag="youth")
    assert any(o["instance"].class_type.key == "kids_7_10" for o in occ)


def test_add_new_class_type_and_schedule_it(app, client, client_account):
    """The Dance example: define a brand-new class in the catalog, place it
    on the weekly schedule, see it live everywhere."""
    from app.models import ClassType

    staff = _admin(app)
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "save_type", "name": "Dance Fit", "age_min": "", "age_max": "",
            "duration_min": "50", "default_capacity": "15", "segment_tag": "",
            "accepts_trials": "on", "active": "on",
        },
        follow_redirects=True,
    )
    assert b"added to the class catalog" in r.data
    dance = db.session.query(ClassType).filter_by(name="Dance Fit").one()
    assert dance.key == "dance_fit"

    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "add_template", "class_type_id": str(dance.id), "cohort": "",
            "weekday": "2", "start_time": "17:30", "capacity": "",
        },
        follow_redirects=True,
    )
    assert b"repeats every Wednesday at 5:30 PM" in r.data
    assert b"Dance Fit" in staff.get("/ops/schedule").data  # live schedule
    assert b"Dance Fit" in client.get("/schedule").data     # public schedule

    # deactivating the class type clears it back off the calendar
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "save_type", "type_id": str(dance.id), "name": "Dance Fit",
            "age_min": "", "age_max": "", "duration_min": "50",
            "default_capacity": "15", "segment_tag": "", "accepts_trials": "on",
            # active checkbox omitted → inactive
        },
        follow_redirects=True,
    )
    assert b"upcoming classes removed" in r.data
    assert b"Dance Fit" not in staff.get("/ops/schedule").data


def test_add_weekly_class_on_multiple_days(app, client, client_account):
    """Group-style scheduling: one add places the class on several days
    (e.g. Mon/Wed/Fri) in a single action."""
    from app.models import ClassType, ScheduleTemplate

    staff = _admin(app)
    she = ClassType(
        client_account_id=client_account.id, key="she_hits", name="She Hits",
        segment_tag="shehits", duration_min=60, default_capacity=16,
    )
    db.session.add(she)
    db.session.commit()

    from werkzeug.datastructures import MultiDict

    r = staff.post(
        "/ops/schedule-builder",
        data=MultiDict(
            [
                ("action", "add_template"),
                ("class_type_id", str(she.id)), ("cohort", ""),
                ("weekdays", "0"), ("weekdays", "2"), ("weekdays", "4"),
                ("start_time", "18:00"), ("capacity", ""), ("trainer_id", ""),
            ]
        ),
        follow_redirects=True,
    )
    assert b"repeats every Monday, Wednesday, Friday at 6:00 PM" in r.data
    tpls = db.session.query(ScheduleTemplate).filter_by(class_type_id=she.id).all()
    assert sorted(t.weekday for t in tpls) == [0, 2, 4]
    assert all(t.start_time_local.hour == 18 for t in tpls)

    # occurrences exist on all three weekdays
    rows = db.session.query(ClassInstance).filter_by(class_type_id=she.id).all()
    assert {i.local_date.weekday() for i in rows} == {0, 2, 4}

    # no days picked → friendly error
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "add_template", "class_type_id": str(she.id),
            "cohort": "", "start_time": "18:00", "capacity": "",
        },
        follow_redirects=True,
    )
    assert b"Pick at least one day" in r.data


def test_coach_login_invite_and_phone_view(app, client, client_account):
    """Trainers become gatekeepers: invite from the Trainers page, sign in,
    land on their own class view, mark attendance — but the wider back
    office stays desk-only."""
    import re

    from datetime import time as dtime

    from app.models import ClassType, Role, ScheduleTemplate, Trainer, User
    from app.services.tzutil import local_to_utc, today_local

    staff = _admin(app)
    trainer = db.session.query(Trainer).first()
    trainer.email = "coach.gate@test.local"
    db.session.commit()

    # invite creates the trainer login + emails a set-password link
    r = staff.post(
        "/ops/trainers",
        data={"action": "invite", "trainer_id": str(trainer.id)},
        follow_redirects=True,
    )
    assert b"Login invite sent" in r.data
    coach_user = db.session.query(User).filter_by(email="coach.gate@test.local").one()
    assert coach_user.role == Role.trainer.value
    db.session.refresh(trainer)
    assert trainer.user_id == coach_user.id
    invite = db.session.query(Message).filter_by(template="coach_invite").one()
    set_url = re.search(r"/portal/set-password/[\w\-\.]+", invite.body_preview).group(0)

    # setting the password signs the coach in and lands on the COACH view,
    # never the member portal
    cbrowser = app.test_client()
    r = cbrowser.post(set_url, data={"password": "coachpass123"}, follow_redirects=False)
    assert "/ops/coach" in r.headers["Location"]
    r = cbrowser.get("/portal/", follow_redirects=False)
    assert "/ops/coach" in r.headers["Location"]  # portal bounces staff home
    cbrowser.get("/portal/logout")
    r = cbrowser.post(
        "/ops/login",
        data={"email": "coach.gate@test.local", "password": "coachpass123"},
        follow_redirects=False,
    )
    assert "/ops/coach" in r.headers["Location"]

    # give the coach a class TODAY with a booking
    kids = db.session.query(ClassType).filter_by(key="kids_7_10").one()
    inst = ClassInstance(
        client_account_id=client_account.id, class_type_id=kids.id,
        trainer_id=trainer.id, cohort_label="Group A",
        starts_at_utc=local_to_utc(today_local(), dtime(23, 50)),
        local_date=today_local(), local_time=dtime(23, 50),
        duration_min=45, capacity=12,
    )
    db.session.add(inst)
    db.session.commit()
    other = _first_instance(client_account, "kids_7_10")
    _book_child(client, other)  # creates guardian+child; rebook onto today
    booking = db.session.query(Booking).one()
    booking.class_instance_id = inst.id
    db.session.commit()

    # a class later in the week shows up collapsed under Upcoming
    from datetime import timedelta

    later = ClassInstance(
        client_account_id=client_account.id, class_type_id=kids.id,
        trainer_id=trainer.id, cohort_label="Group A",
        starts_at_utc=local_to_utc(today_local() + timedelta(days=3), dtime(16, 0)),
        local_date=today_local() + timedelta(days=3), local_time=dtime(16, 0),
        duration_min=45, capacity=12,
    )
    db.session.add(later)
    db.session.commit()

    r = cbrowser.get("/ops/coach")
    assert b"Maya" in r.data
    assert b"Here" in r.data  # the big attendance button
    assert b"Upcoming" in r.data
    assert later.local_date.strftime("%A").encode() in r.data

    # coach marks attendance → bounced back to coach view, booking attended
    r = cbrowser.post(
        f"/ops/bookings/{booking.id}/attendance",
        data={"action": "attended"},
        follow_redirects=False,
    )
    assert "/ops/coach" in r.headers["Location"]
    db.session.refresh(booking)
    assert booking.status == BookingStatus.attended.value

    # the rest of the back office is closed to coaches
    for path in ("/ops/today", "/ops/members", "/ops/schedule-builder",
                 "/ops/kiosk", "/ops/announcements"):
        assert cbrowser.get(path).status_code == 403, path


def test_marketing_report_page(app, client, client_account, tmp_path, monkeypatch):
    """Marketing page: parses the access log, filters Meta bot ranges and
    internal test links, tracks funnel steps per visitor, saves spend, and
    stays admin-only."""
    log = tmp_path / "access.log"
    ua = "Mozilla/5.0 (iPhone)"
    log.write_text(
        # human: lands on c1, opens picker, reaches details
        f'9.9.9.9 - - [27/Aug/2026:10:00:00 -0700] "GET /kids?v=c1&utm_source=meta&utm_campaign=kids HTTP/1.1" 200 1 "-" "{ua}"\n'
        f'9.9.9.9 - - [27/Aug/2026:10:01:00 -0700] "GET /book/kids HTTP/1.1" 200 1 "-" "{ua}"\n'
        f'9.9.9.9 - - [27/Aug/2026:10:02:00 -0700] "GET /book/kids/details HTTP/1.1" 200 1 "-" "{ua}"\n'
        # human: c3 bounce
        f'8.8.4.4 - - [27/Aug/2026:11:00:00 -0700] "GET /kids?v=c3&utm_source=meta&utm_campaign=kids HTTP/1.1" 200 1 "-" "{ua}"\n'
        # Meta bot fleet — must be excluded
        f'173.252.70.5 - - [27/Aug/2026:10:00:00 -0700] "GET /kids?v=c1&utm_source=meta HTTP/1.1" 200 1 "-" "{ua}"\n'
        f'7.7.7.7 - - [27/Aug/2026:10:00:00 -0700] "GET /kids?v=c1&utm_source=meta HTTP/1.1" 200 1 "-" "facebookexternalhit/1.1"\n'
        # internal test link — excluded
        f'6.6.6.6 - - [27/Aug/2026:10:00:00 -0700] "GET /kids?v=c1&utm_source=meta&fbclid=fbclid HTTP/1.1" 200 1 "-" "{ua}"\n'
    )
    monkeypatch.setenv("NGINX_ACCESS_LOG", str(log))

    from app.services.marketing_report import traffic_funnel

    t = traffic_funnel("kids")
    assert t["totals"] == {"visitors": 2, "landings": 2, "picker": 1, "details": 1}
    assert t["per_ad"]["c1"]["details"] == 1
    assert t["per_ad"]["c3"]["visitors"] == 1

    staff = _admin(app)
    r = staff.get("/ops/marketing")
    assert r.status_code == 200
    assert b"Funnel by ad" in r.data

    r = staff.post(
        "/ops/marketing", data={"action": "spend", "spend": "11.60"},
        follow_redirects=True,
    )
    assert b"$11.60" in r.data

    # campaign switcher: youth tab works, junk falls back to kids
    assert b"utm" not in staff.get("/ops/marketing?c=youth").data  # renders
    assert staff.get("/ops/marketing?c=youth").status_code == 200
    assert staff.get("/ops/marketing?c=evil").status_code == 200

    # admin-only: front desk is refused
    fd = app.test_client()
    fd.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    assert fd.get("/ops/marketing").status_code == 403


def test_delete_trainer_unassigns_and_removes(app, client, client_account):
    from app.models import ScheduleTemplate, Trainer

    staff = _admin(app)
    trainer = db.session.query(Trainer).first()  # seeded, assigned to templates
    tid = trainer.id
    r = staff.post(
        "/ops/trainers",
        data={"action": "delete", "trainer_id": str(tid)},
        follow_redirects=True,
    )
    assert b"deleted and unassigned" in r.data
    assert db.session.get(Trainer, tid) is None
    assert (
        db.session.query(ScheduleTemplate).filter_by(trainer_id=tid).count() == 0
    )
    assert db.session.query(ClassInstance).filter_by(trainer_id=tid).count() == 0


def test_bulk_assign_coach_and_daily_override(app, client, client_account):
    from app.models import ScheduleTemplate, Trainer

    staff = _admin(app)
    frankie = Trainer(
        client_account_id=client_account.id, name="Coach Frankie", active=True
    )
    db.session.add(frankie)
    # clear existing assignments so "unassigned" scope covers everything
    for tpl in db.session.query(ScheduleTemplate).all():
        tpl.trainer_id = None
    for inst in db.session.query(ClassInstance).all():
        inst.trainer_id = None
    db.session.commit()

    # 1. bulk assign to all unassigned classes — conflicts (same-time kids/
    #    teens templates) are skipped and reported, not silently doubled
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "bulk_assign",
            "bulk_trainer_id": str(frankie.id),
            "scope": "unassigned",
        },
        follow_redirects=True,
    )
    assert b"Coach Frankie assigned to 1 weekly classes" in r.data
    assert b"Skipped (time overlaps)" in r.data  # the overlapping second slot
    assigned = [
        t for t in db.session.query(ScheduleTemplate).all() if t.trainer_id == frankie.id
    ]
    assert len(assigned) == 1
    inst = (
        db.session.query(ClassInstance)
        .filter_by(template_id=assigned[0].id)
        .first()
    )
    assert inst.trainer_id == frankie.id  # future occurrences follow

    # 2. daily override: swap the coach for ONE day only, with a booking
    sub = Trainer(client_account_id=client_account.id, name="Coach Daysub", active=True)
    db.session.add(sub)
    db.session.commit()
    target = _first_instance(client_account, "kids_7_10")
    _book_child(client, target)
    tpl_id = target.template_id
    tpl_trainer_before = db.session.get(ScheduleTemplate, tpl_id).trainer_id

    r = staff.post(
        f"/ops/instances/{target.id}/coach",
        data={"trainer_id": str(sub.id)},
        follow_redirects=True,
    )
    assert b"this day only" in r.data
    db.session.refresh(target)
    assert target.trainer_id == sub.id
    # the weekly default did NOT change
    assert db.session.get(ScheduleTemplate, tpl_id).trainer_id == tpl_trainer_before
    # other occurrences of the same template untouched
    others = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.template_id == tpl_id, ClassInstance.id != target.id
        )
        .all()
    )
    assert all(i.trainer_id != sub.id for i in others)
    # booked member notified of the substitution
    assert db.session.query(Message).filter_by(template="sub_notice").count() >= 1

    # 3. scope=all: same control makes the coach the weekly default going
    # forward — template updated, all future occurrences follow
    r = staff.post(
        f"/ops/instances/{target.id}/coach",
        data={"trainer_id": str(sub.id), "scope": "all"},
        follow_redirects=True,
    )
    assert b"every" in r.data
    assert db.session.get(ScheduleTemplate, tpl_id).trainer_id == sub.id
    future = (
        db.session.query(ClassInstance)
        .filter(
            ClassInstance.template_id == tpl_id,
            ClassInstance.local_date >= target.local_date,
        )
        .all()
    )
    assert future and all(i.trainer_id == sub.id for i in future)

    # 4. scope=all with Coach TBA removes the coach from every week
    r = staff.post(
        f"/ops/instances/{target.id}/coach",
        data={"trainer_id": "", "scope": "all"},
        follow_redirects=True,
    )
    assert b"coach set to TBA" in r.data
    assert db.session.get(ScheduleTemplate, tpl_id).trainer_id is None
    for i in future:
        db.session.refresh(i)
        assert i.trainer_id is None


def test_move_weekly_class_time_keeps_bookings_and_notifies(
    app, client, client_account
):
    from datetime import time as dtime

    from app.models import ScheduleTemplate

    instance = _first_instance(client_account, "kids_7_10")
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    tpl = db.session.get(ScheduleTemplate, instance.template_id)
    # clear the seeded coach off the OTHER template so the move can't hit the
    # (correct) trainer-overlap rejection
    for other in db.session.query(ScheduleTemplate).all():
        if other.id != tpl.id:
            other.trainer_id = None
    db.session.commit()
    old_date = instance.local_date
    new_time = dtime(tpl.start_time_local.hour, 30)  # +30 min, same day

    staff = _admin(app)
    r = staff.post(
        "/ops/schedule-builder",
        data={
            "action": "save_template", "template_id": str(tpl.id),
            "weekday": str(tpl.weekday),
            "start_time": new_time.strftime("%H:%M"),
            "cohort": tpl.cohort_label or "", "capacity": "",
            "trainer_id": str(tpl.trainer_id or ""),
        },
        follow_redirects=True,
    )
    assert b"moved to the new day/time" in r.data

    db.session.refresh(instance)
    db.session.refresh(booking)
    assert instance.local_time == new_time      # occurrence moved
    assert instance.local_date == old_date
    assert booking.status == BookingStatus.booked.value  # booking intact
    change = db.session.query(Message).filter_by(template="schedule_change").all()
    assert {m.channel for m in change} == {"email", "sms"}


def test_today_hides_and_prunes_legacy_inactive_template_instances(
    app, client, client_account
):
    """Instances generated BEFORE their template was deactivated (pre-update
    orphans) must not show on Today/kiosk, and the prune sweep removes them."""
    from datetime import time as dtime

    from app.models import ClassType, ScheduleTemplate
    from app.services.class_admin import prune_inactive_template_instances
    from app.services.tzutil import local_to_utc, today_local

    ct = ClassType(
        client_account_id=client_account.id,
        key="legacy", name="Legacy Orphan Boxing", segment_tag="strong",
        duration_min=45, default_capacity=10,
    )
    db.session.add(ct)
    db.session.flush()
    tpl = ScheduleTemplate(
        client_account_id=client_account.id, class_type_id=ct.id,
        weekday=today_local().weekday(), start_time_local=dtime(23, 50),
        active=False,  # deactivated — but its instance below already exists
    )
    db.session.add(tpl)
    db.session.flush()
    orphan = ClassInstance(
        client_account_id=client_account.id, template_id=tpl.id,
        class_type_id=ct.id, starts_at_utc=local_to_utc(today_local(), dtime(23, 50)),
        local_date=today_local(), local_time=dtime(23, 50),
        duration_min=45, capacity=10,
    )
    db.session.add(orphan)
    db.session.commit()

    staff = _admin(app)
    assert b"Legacy Orphan Boxing" not in staff.get("/ops/today").data
    assert b"Legacy Orphan Boxing" not in staff.get("/ops/schedule").data

    pruned = prune_inactive_template_instances(client_account.id)
    db.session.commit()
    assert pruned == 1
    assert db.session.get(ClassInstance, orphan.id) is None  # empty → deleted


def test_deactivate_trainer_unassigns_everywhere(app, client, client_account):
    from app.models import ScheduleTemplate, Trainer

    staff = _admin(app)
    trainer = db.session.query(Trainer).first()
    assert b"Coach Alex T." in staff.get("/ops/schedule").data

    # save with the Active checkbox unticked → deactivation
    r = staff.post(
        "/ops/trainers",
        data={
            "trainer_id": trainer.id, "name": trainer.name,
            "role_title": "", "certs": "", "bio": "",
        },
        follow_redirects=True,
    )
    assert b"unassigned" in r.data
    assert all(
        t.trainer_id != trainer.id
        for t in db.session.query(ScheduleTemplate).all()
    )
    page = staff.get("/ops/schedule").data
    assert b"Coach Alex T." not in page  # inactive coaches never render
    assert b"Coach TBA" in page  # slots fall back to the TBA option


def test_substitution_notifies_members(app, client, client_account):
    from app.models import Trainer

    staff = _admin(app)
    instance = _first_instance(client_account)
    _book_child(client, instance)
    new_trainer = Trainer(
        client_account_id=client_account.id, name="Coach Sub", active=True
    )
    db.session.add(new_trainer)
    db.session.commit()

    staff.post(
        f"/ops/instances/{instance.id}",
        data={"action": "override", "trainer_id": new_trainer.id, "capacity": instance.capacity},
    )
    db.session.refresh(instance)
    assert instance.trainer_id == new_trainer.id
    assert "sub:" in (instance.notes or "")  # history logged
    notice = db.session.query(Message).filter_by(template="sub_notice").one()
    assert "Coach Sub" in notice.body_preview


def test_today_view_shows_new_signups(app, client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    staff = app.test_client()
    staff.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    r = staff.get("/ops/today")
    assert b"New sign-ups" in r.data
    assert b"Maya" in r.data
    assert b"Guardian: Sam Parent" in r.data
    # shows which day they signed up for (the class date)
    assert instance.local_date.strftime("%a %b %d").encode() in r.data
    # a cancelled signup still shows, chipped as cancelled
    booking = db.session.query(Booking).one()
    booking.status = BookingStatus.cancelled.value
    db.session.commit()
    r = staff.get("/ops/today")
    assert b"cancelled" in r.data


# ------------------------------------------------- directory, notes, flags ---
def test_member_directory_flags_and_notes(app, client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    staff = _admin(app)

    r = staff.get("/ops/members")
    assert b"Sam Parent" in r.data
    assert b"first-timer" in r.data  # no attendance yet → flag

    guardian = db.session.query(User).filter_by(email="sam.parent@example.com").one()
    r = staff.post(
        f"/ops/members/{guardian.id}",
        data={"action": "note", "body": "Maya has a peanut allergy — epipen in bag"},
        follow_redirects=True,
    )
    assert b"peanut allergy" in r.data
    note = db.session.query(MemberNote).one()
    assert note.user_id == guardian.id

    # source attribution visible read-only
    r = staff.get(f"/ops/members/{guardian.id}")
    assert b"source:" in r.data


# --------------------------------------------------- reports, CSV, reviews ---
def test_reports_and_csv_admin_only(app, client, client_account):
    staff = _admin(app)
    r = staff.get("/ops/reports")
    assert r.status_code == 200
    assert b"Fill heatmap" in r.data

    r = staff.get("/ops/export/leads.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert b"utm_campaign" in r.data

    # front desk cannot see reports
    fd = app.test_client()
    fd.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    assert fd.get("/ops/reports").status_code == 403
    assert fd.get("/ops/export/payments.csv").status_code == 403


def test_announcements_portal_banner_and_consented_email(app, client, client_account):
    staff = _admin(app)
    guardian, _ = _make_member(client_account, email="news@example.com")
    guardian.consent_email = True  # CASL consent for the marketing-ish email
    db.session.commit()

    staff.post(
        "/ops/announcements",
        data={"action": "create", "title": "Holiday schedule", "body": "Closed Aug 4."},
    )
    a = db.session.query(Announcement).one()

    # portal banner
    _login(client, guardian.email)
    r = client.get("/portal/")
    assert b"Holiday schedule" in r.data

    # email respects consent + news pref
    staff.post("/ops/announcements", data={"action": "email", "id": a.id})
    sent = db.session.query(Message).filter_by(template="announcement").all()
    assert any(m.recipient == "news@example.com" for m in sent)


def test_review_crud_and_badge(app, client, client_account):
    staff = _admin(app)
    staff.post(
        "/ops/reviews",
        data={
            "action": "save", "reviewer_name": "New P.", "rating": "5",
            "quote_text": "Verbatim words here.", "segment_tags": "youth",
            "display_order": "1", "active": "on",
        },
    )
    assert db.session.query(Review).filter_by(reviewer_name="New P.").count() == 1
    staff.post(
        "/ops/reviews",
        data={"action": "badge", "google_rating": "4.9", "google_review_count": "31"},
    )
    r = client.get("/youth")
    assert b"4.9" in r.data and b"31 Google reviews" in r.data


# ------------------------------------------------------- tracking + calls ---
def test_outbox_dispatch_and_hashing(app, client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    pending = db.session.query(EventOutbox).filter(
        EventOutbox.dispatched_at.is_(None)
    ).count()
    assert pending >= 2  # Lead + Schedule

    # unconfigured destinations → marked dispatched with note (dev behavior)
    with app.test_request_context():
        done = drain()
    db.session.commit()
    assert done == pending
    assert (
        db.session.query(EventOutbox).filter(EventOutbox.dispatched_at.is_(None)).count()
        == 0
    )

    hashed = hash_user_data({"email": " Sam.Parent@Example.com ", "phone": "+16045550123"})
    assert hashed["em"][0] == __import__("hashlib").sha256(
        b"sam.parent@example.com"
    ).hexdigest()
    assert hashed["ph"][0] == __import__("hashlib").sha256(b"16045550123").hexdigest()


def test_call_webhook_matching_and_attribution(client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    lead = db.session.query(Lead).one()
    lead.status = "activated"
    db.session.commit()

    r = client.post(
        "/api/v1/webhooks/calls",
        json={
            "customer_phone_number": "604-555-0123",
            "tracking_phone_number": "+16040001111",
            "duration": "95",
            "start_time": "2026-07-24T10:00:00Z",
        },
    )
    data = r.get_json()
    assert data["received"] is True
    call = db.session.query(Call).one()
    assert call.matched_lead_id == lead.id
    assert call.caller_number == "+16045550123"
    events = {e.event_name for e in db.session.query(EventOutbox).all()}
    assert "CallAttributedConversion" in events


# --------------------------------------------------------- FINAL E2E gate ---
def test_final_full_platform_e2e(app, client, client_account, monkeypatch):
    """funnel booking → activation → portal login → class book → kiosk
    check-in → attendance → reporting + command-center numbers reconcile."""
    # 1. funnel: ad click + child booking
    client.get("/youth?utm_source=meta&utm_campaign=final&utm_content=final-A")
    instance = _first_instance(client_account)
    _book_child(client, instance)
    guardian = db.session.query(User).filter_by(email="sam.parent@example.com").one()
    booking = db.session.query(Booking).one()
    _vault_card(guardian)

    # 2. attend + activate + first invoice paid
    staff = app.test_client()
    staff.post("/ops/login", data={"email": "frontdesk@test.local", "password": "pw"})
    staff.post(f"/ops/bookings/{booking.id}/attendance", data={"action": "attended"})
    staff.post(f"/ops/bookings/{booking.id}/activate")
    sub = db.session.query(Subscription).one()
    sub.stripe_subscription_id = "sub_final"
    db.session.commit()
    _webhook(client, "invoice.paid", {
        "id": "in_final_1", "subscription": "sub_final",
        "amount_paid": 18900, "currency": "cad", "charge": "ch_final_1",
    })

    # 3. portal: set password via the welcome invite, then book next class
    welcome = db.session.query(Message).filter_by(template="membership_welcome").one()
    invite = re.search(r"/portal/set-password/[\w\-\.]+", welcome.body_preview).group(0)
    r = client.post(invite, data={"password": "newpassword1"}, follow_redirects=False)
    assert r.status_code == 302  # logged in

    next_inst = [
        o["instance"]
        for o in __import__(
            "app.services.scheduling", fromlist=["upcoming_instances"]
        ).upcoming_instances(client_account.id, segment_tag="youth")
        if o["instance"].class_type.key == "kids_7_10" and o["instance"].id != instance.id
    ]
    if not next_inst:  # only one generated occurrence — create the next week's
        from datetime import time as dtime
        from app.services.tzutil import local_to_utc, today_local

        d = instance.local_date + timedelta(days=7)
        ni = ClassInstance(
            client_account_id=client_account.id,
            class_type_id=instance.class_type_id,
            cohort_label="Group A",
            starts_at_utc=local_to_utc(d, dtime(11, 0)),
            local_date=d, local_time=dtime(11, 0), duration_min=45, capacity=12,
        )
        db.session.add(ni)
        db.session.commit()
        next_inst = [ni]
    child = booking.attendee
    r = client.post(
        "/portal/bookings",
        data={"instance_id": next_inst[0].id, "attendee_id": child.id},
        follow_redirects=True,
    )
    assert b"booked" in r.data.lower()
    member_booking = (
        db.session.query(Booking)
        .filter_by(attendee_id=child.id, class_instance_id=next_inst[0].id)
        .one()
    )
    assert member_booking.kind == "member"

    # 4. kiosk check-in for the member booking (move class to today)
    from datetime import time as dtime
    from app.services.tzutil import local_to_utc, today_local

    ni = next_inst[0]
    ni.local_date = today_local()
    ni.local_time = dtime(23, 57)
    ni.starts_at_utc = local_to_utc(today_local(), dtime(23, 57))
    db.session.commit()
    r = staff.post("/ops/kiosk/search", data={"q": "maya"})
    assert b"Maya" in r.data
    r = staff.post(f"/ops/kiosk/checkin/{member_booking.id}")
    assert b"Welcome back" in r.data  # member now, not first-timer
    db.session.refresh(member_booking)
    assert member_booking.status == BookingStatus.attended.value

    # 5. reporting + command-center numbers reconcile
    admin = _admin(app)
    r = admin.get("/ops/reports")
    assert r.status_code == 200
    payment = db.session.query(Payment).one()
    assert payment.agency_share_cents == round(18900 * client_account.commission_rate)
    assert db.session.query(Lead).one().status == "activated"
    assert sub.status == SubscriptionStatus.active.value
    # trial conversion shows 1 booked / 1 showed / 1 converted for Kids Boxing
    assert b"Kids Boxing" in r.data

    # CSV exports reconcile with the DB
    r = admin.get("/ops/export/payments.csv")
    assert b"in_final_1" in r.data
    assert str(payment.agency_share_cents).encode() in r.data
