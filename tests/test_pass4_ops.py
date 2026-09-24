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

from test_pass1_e2e import _book_child, _child_form, _first_instance
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


def test_trial_followup_nudge_and_call_list(app, client, client_account):
    """Attended trials without a membership get exactly one next-day email
    nudge (TRIAL_FOLLOWUP_DAYS), and appear on the Today page's follow-up
    call list."""
    from datetime import time as dtime, timedelta

    from app.models import ClassType
    from app.services.tzutil import local_to_utc, today_local
    from app.tasks.jobs import send_trial_followups

    # attended trial exactly TRIAL_FOLLOWUP_DAYS ago, no membership
    kids = db.session.query(ClassType).filter_by(key="kids_7_10").one()
    past = today_local() - timedelta(days=app.config["TRIAL_FOLLOWUP_DAYS"])
    inst = ClassInstance(
        client_account_id=client_account.id, class_type_id=kids.id,
        cohort_label="Group A",
        starts_at_utc=local_to_utc(past, dtime(16, 0)),
        local_date=past, local_time=dtime(16, 0), duration_min=45, capacity=12,
    )
    db.session.add(inst)
    db.session.commit()
    other = _first_instance(client_account, "kids_7_10")
    _book_child(client, other)
    booking = db.session.query(Booking).one()
    booking.class_instance_id = inst.id
    booking.status = BookingStatus.attended.value
    db.session.commit()

    assert send_trial_followups.apply().get() == 1
    nudge = db.session.query(Message).filter_by(
        template="trial_followup", channel="email"
    ).one()
    assert "/activate/" in nudge.body_preview
    assert "still open" in nudge.subject

    # strictly once
    assert send_trial_followups.apply().get() == 0

    # and the Today page lists them for the front desk
    staff = _admin(app)
    r = staff.get("/ops/today")
    assert b"Trial follow-ups" in r.data
    assert b"Maya" in r.data


def test_staff_cancel_booking_from_member_page(app, client, client_account):
    """Ops member page can cancel a trial booking (e.g. an emailed
    cancellation request) — spot released, status recorded."""
    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian_id = booking.attendee.user_id

    staff = _admin(app)
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={"action": "cancel_booking", "booking_id": str(booking.id)},
        follow_redirects=True,
    )
    assert b"Booking cancelled" in r.data
    db.session.refresh(booking)
    assert booking.status == BookingStatus.cancelled.value
    assert booking.cancelled_at is not None

    # cancelling again is refused gracefully
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={"action": "cancel_booking", "booking_id": str(booking.id)},
        follow_redirects=True,
    )
    assert b"cannot be cancelled" in r.data


def test_template_starts_on_gates_generation(app, client_account):
    """A template with starts_on (program launch date) generates no
    instances before that date — used when a class is announced ahead of
    its first session."""
    from datetime import timedelta

    from app.models import ClassInstance, ScheduleTemplate
    from app.services.scheduling import generate_instances
    from app.services.tzutil import today_local

    tpl = db.session.query(ScheduleTemplate).first()
    launch = today_local() + timedelta(days=7)
    # Gate the whole program, as production does (every template of the
    # class type), so the picker window test below sees a clean launch.
    siblings = (
        db.session.query(ScheduleTemplate)
        .filter_by(class_type_id=tpl.class_type_id)
        .all()
    )
    for t in siblings:
        t.starts_on = launch
        db.session.query(ClassInstance).filter_by(template_id=t.id).delete()
    db.session.commit()

    generate_instances(client_account.id)
    db.session.commit()
    dates = [
        i.local_date
        for i in db.session.query(ClassInstance).filter_by(template_id=tpl.id)
    ]
    assert dates, "expected instances after the launch date"
    assert min(dates) >= launch

    # The picker window anchors at the launch date: a program starting
    # beyond today+14 still shows a full run, not just its opening day.
    from app.services.scheduling import upcoming_instances

    shown = [
        o["instance"].local_date
        for o in upcoming_instances(
            client_account.id, class_type_id=tpl.class_type_id
        )
    ]
    assert shown and min(shown) == min(dates)
    assert max(shown) >= launch + timedelta(days=10)


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


def test_call_webhook_matching_and_attribution(app, client, client_account):
    instance = _first_instance(client_account)
    _book_child(client, instance)
    lead = db.session.query(Lead).one()
    lead.status = "activated"
    db.session.commit()

    # No/wrong token → closed (the endpoint requires the shared secret)
    app.config["CALLS_WEBHOOK_TOKEN"] = "test-token"
    assert client.post("/api/v1/webhooks/calls", json={}).status_code == 403

    r = client.post(
        "/api/v1/webhooks/calls?token=test-token",
        json={
            "customer_phone_number": "604-330-2671",
            "tracking_phone_number": "+16040001111",
            "duration": "95",
            "start_time": "2026-07-24T10:00:00Z",
        },
    )
    data = r.get_json()
    assert data["received"] is True
    call = db.session.query(Call).one()
    assert call.matched_lead_id == lead.id
    assert call.caller_number == "+16043302671"
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


def test_retroactive_checkin_from_member_page(app, client, client_account):
    """Coaches often skip live attendance marking, so an unmarked trial
    silently no-shows and the guardian never gets the payment link. The
    member page can check them in after the fact, re-firing the whole
    post-class flow (activation email / auto-start)."""
    from datetime import timedelta

    from app.models import Message, utcnow
    from app.services.tzutil import today_local

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian_id = booking.attendee.user_id
    # The class happened yesterday and the automark flagged a no-show.
    booking.class_instance.starts_at_utc = utcnow() - timedelta(days=1)
    booking.class_instance.local_date = today_local() - timedelta(days=1)
    booking.status = BookingStatus.no_show.value
    db.session.commit()

    staff = _admin(app)
    r = staff.get(f"/ops/members/{guardian_id}")
    assert b"Mark attended" in r.data

    r = staff.post(
        f"/ops/bookings/{booking.id}/attendance",
        data={"action": "attended", "return": "member"},
        follow_redirects=True,
    )
    assert b"marked attended" in r.data  # flash, back on the member page
    db.session.refresh(booking)
    assert booking.status == BookingStatus.attended.value
    assert db.session.get(Lead, booking.lead_id).status == "attended"
    sent = (
        db.session.query(Message)
        .filter_by(template="post_class", channel="email")
        .count()
    )
    assert sent >= 1

    # "I never got it": the member page can re-send the activation email
    r = staff.get(f"/ops/members/{guardian_id}")
    assert b"Resend membership email" in r.data
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "resend_membership_email",
            "booking_id": str(booking.id),
        },
        follow_redirects=True,
    )
    assert b"re-sent" in r.data
    assert (
        db.session.query(Message)
        .filter_by(template="post_class", channel="email")
        .count()
        == sent + 1
    )
    # the staff resend also composes the SMS with the activation link,
    # as transactional (the guardian asked for it)
    sms = (
        db.session.query(Message)
        .filter_by(template="post_class", channel="sms")
        .order_by(Message.id.desc())
        .first()
    )
    assert sms is not None and "/activate/" in sms.body_preview
    assert sms.delivery_status != "suppressed_no_consent"


def test_first_charge_date_override(app, client, client_account):
    """Staff-agreed payday ("take it on the 21st"): the date is set on the
    trial booking from the member page; activation charges on that date and
    the pre-charge reminder states the date instead of "in 48 hours"."""
    from datetime import timedelta

    from app.models import PaymentMethodStatus
    from app.services import billing
    from app.services.tzutil import today_local

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian = booking.attendee.guardian
    payday = today_local() + timedelta(days=17)

    staff = _admin(app)
    r = staff.post(
        f"/ops/members/{guardian.id}",
        data={
            "action": "set_first_charge",
            "booking_id": str(booking.id),
            "first_charge_on": payday.isoformat(),
        },
        follow_redirects=True,
    )
    assert b"First charge set for" in r.data
    db.session.refresh(booking)
    assert booking.first_charge_on == payday

    # a past date is refused
    r = staff.post(
        f"/ops/members/{guardian.id}",
        data={
            "action": "set_first_charge",
            "booking_id": str(booking.id),
            "first_charge_on": (today_local() - timedelta(days=1)).isoformat(),
        },
        follow_redirects=True,
    )
    assert b"future date" in r.data

    db.session.add(
        StripeCustomer(
            user_id=guardian.id,
            payment_method_status=PaymentMethodStatus.vaulted.value,
        )
    )
    db.session.commit()
    with app.test_request_context():
        sub = billing.activate_subscription(
            booking.attendee,
            cohort_label=None,
            first_charge_on=booking.first_charge_on,
        )
    db.session.commit()
    assert sub.first_charge_at.date() == payday
    reminder = (
        db.session.query(Message)
        .filter_by(template="pre_charge_reminder", channel="email")
        .one()
    )
    assert payday.strftime("%B") in reminder.subject
    assert "hours" not in reminder.subject


def test_stop_trial_followups(app, client, client_account):
    """A family that bows out (injury, changed mind) gets an off switch:
    Stop follow-ups silences the nudge email and removes them from the
    Today call list; Resume undoes it."""
    from datetime import time as dtime, timedelta

    from app.models import ClassType
    from app.services.tzutil import local_to_utc, today_local
    from app.tasks.jobs import send_trial_followups

    kids = db.session.query(ClassType).filter_by(key="kids_7_10").one()
    past = today_local() - timedelta(days=app.config["TRIAL_FOLLOWUP_DAYS"])
    inst = ClassInstance(
        client_account_id=client_account.id, class_type_id=kids.id,
        starts_at_utc=local_to_utc(past, dtime(16, 0)),
        local_date=past, local_time=dtime(16, 0), duration_min=45, capacity=12,
    )
    db.session.add(inst)
    db.session.commit()
    other = _first_instance(client_account, "kids_7_10")
    _book_child(client, other)
    booking = db.session.query(Booking).one()
    booking.class_instance_id = inst.id
    booking.status = BookingStatus.attended.value
    db.session.commit()
    guardian_id = booking.attendee.user_id

    staff = _admin(app)
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "close_trial",
            "attendee_id": str(booking.attendee_id),
            "reason": "broken wrist",
        },
        follow_redirects=True,
    )
    assert b"Follow-ups stopped" in r.data
    assert b"broken wrist" in r.data  # reason chip on the member page

    # nudge suppressed, call list empty
    assert send_trial_followups.apply().get() == 0
    assert b"Trial follow-ups" not in staff.get("/ops/today").data

    # resume brings the machinery back
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={"action": "reopen_trial", "attendee_id": str(booking.attendee_id)},
        follow_redirects=True,
    )
    assert b"Follow-ups resumed" in r.data
    assert send_trial_followups.apply().get() == 1
    assert b"Trial follow-ups" in staff.get("/ops/today").data


def test_card_save_return_shows_success_and_next_step(
    app, client, client_account, monkeypatch
):
    """Stripe bounces back to the card page after a save; that landing must
    say the card saved and hand over the one remaining tap (start the
    membership) - not re-render a blank form (prod confusion 2026-09-08)."""
    from app.services import stripe_service
    from app.services.signed_links import SALT_UPDATE_CARD, make_token

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    booking.status = BookingStatus.attended.value
    guardian = booking.attendee.guardian
    sc = StripeCustomer(user_id=guardian.id, stripe_customer_id="cus_test1")
    db.session.add(sc)
    db.session.commit()

    class FakeSI:
        status = "succeeded"
        customer = "cus_test1"
        payment_method = "pm_test1"

    class FakeStripe:
        class SetupIntent:
            @staticmethod
            def retrieve(_id):
                return FakeSI()

    monkeypatch.setattr(stripe_service, "is_configured", lambda: True)
    monkeypatch.setattr(stripe_service, "stripe_client", lambda: FakeStripe)

    with app.test_request_context():
        tok = make_token(guardian.id, SALT_UPDATE_CARD)
    r = client.get(
        f"/portal/card/{tok}?setup_intent=seti_1&redirect_status=succeeded"
    )
    assert b"Your card is saved" in r.data
    assert b"/activate/" in r.data  # the onward tap
    db.session.refresh(sc)
    assert sc.payment_method_status == "vaulted"


def test_edit_attendee_name_and_birth_year(app, client, client_account):
    """Front desk can fix a child's name/birth year (browser autofill
    mangles these). First name is required; a bad birth year is ignored."""
    from app.models import AttendeeProfile

    instance = _first_instance(client_account)
    _book_child(client, instance)
    child = db.session.query(AttendeeProfile).filter_by(kind="child").one()
    guardian_id = child.user_id
    assert child.first_name == "Maya"

    staff = _admin(app)
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "edit_attendee",
            "attendee_id": str(child.id),
            "first_name": "Mia",
            "last_name": "Parent",
            "birth_year": "2016",
        },
        follow_redirects=True,
    )
    assert b"Details updated" in r.data
    db.session.refresh(child)
    assert child.first_name == "Mia"
    assert child.last_name == "Parent"
    assert child.birth_year == 2016

    # blank first name refused
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "edit_attendee",
            "attendee_id": str(child.id),
            "first_name": "  ",
        },
        follow_redirects=True,
    )
    assert b"first name is required" in r.data
    db.session.refresh(child)
    assert child.first_name == "Mia"  # unchanged

    # nonsense birth year ignored, name still saved
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "edit_attendee",
            "attendee_id": str(child.id),
            "first_name": "Mia",
            "birth_year": "3999",
        },
        follow_redirects=True,
    )
    db.session.refresh(child)
    assert child.birth_year == 2016


def test_ad_invite_flow_attributes_to_campaign(app, client, client_account):
    """Off-flow ad enquiries: staff invite by email; the signed link seeds
    campaign attribution server-side; a booking through it becomes a lead
    tagged meta / referral / <segment> / gym-referral (transparent it was a
    gym referral, not a pixel click) with no phantom lead created up front."""
    from app.models import Message

    staff = _admin(app)
    r = staff.post(
        "/ops/members/invite",
        data={
            "email": "Walkin.Parent@Example.com",
            "first_name": "Jordan",
            "segment": "kids",
        },
        follow_redirects=True,
    )
    assert b"Invite sent" in r.data
    invite = (
        db.session.query(Message).filter_by(template="ad_invite").one()
    )
    assert invite.recipient == "walkin.parent@example.com"
    # no lead conjured before they actually book
    assert db.session.query(Lead).count() == 0
    link = re.search(r"/invite/([\w.\-]+)", invite.body_preview).group(0)

    # The invited person clicks the link → attribution cookie seeded, then
    # books a class through the normal funnel.
    visitor = app.test_client()
    r = visitor.get(link)
    assert r.status_code == 302 and "/kids" in r.location
    instance = _first_instance(client_account, "kids_7_10")
    r = visitor.post("/book/youth", data={"instance_id": str(instance.id)})
    assert r.status_code == 302
    r = visitor.post(
        "/book/youth/details", data=_child_form(instance), follow_redirects=False
    )
    assert r.status_code == 302

    lead = db.session.query(Lead).one()
    assert lead.utm_source == "meta"
    assert lead.utm_medium == "referral"
    assert lead.utm_campaign == "kids"
    assert lead.utm_content == "gym-referral"

    # a tampered/garbage invite token 404s
    assert visitor.get("/invite/not-a-real-token").status_code == 404


def test_payment_health_check_flags_and_alerts(app, client, client_account, monkeypatch):
    """The daily self-audit flags a Stripe-charged invoice with no local
    Payment (the silent-webhook failure) and emails the ops mailbox; it
    stays silent when everything reconciles."""
    import json as _json

    from app.models import Payment, Subscription, SubscriptionStatus, Plan
    from app.services import stripe_service
    from app.tasks import jobs

    # A subscription that Stripe says was charged, but we recorded nothing.
    guardian, child = _make_member(client_account, email="audit@example.com")
    sub = db.session.query(Subscription).filter_by(user_id=guardian.id).one()
    sub.stripe_subscription_id = "sub_audit1"
    db.session.commit()

    class FakeInvoice:
        def __init__(self, d):
            self._d = d
        def __str__(self):
            return _json.dumps(self._d)

    class FakeList:
        def __init__(self, items):
            self._items = items
        def auto_paging_iter(self):
            return iter(self._items)

    paid = FakeInvoice({"id": "in_audit1", "amount_paid": 19845, "tax": 945})

    class FakeStripe:
        api_version = None
        class Invoice:
            @staticmethod
            def list(**kw):
                return FakeList([paid])

    monkeypatch.setattr(stripe_service, "is_configured", lambda: True)
    monkeypatch.setattr(stripe_service, "stripe_client", lambda: FakeStripe)

    sent = []
    monkeypatch.setattr(
        jobs, "send_email",
        lambda *a, **k: sent.append((a[1], a[2])),  # (recipient, subject)
    )
    monkeypatch.setitem(app.config, "ADMIN_NOTIFY_EMAIL", "ops@box2fit.local")

    n = jobs.payment_health_check()
    assert n >= 1
    assert sent and "payment issue" in sent[0][1].lower()

    # Now record the payment — the audit goes quiet.
    db.session.add(
        Payment(
            client_account_id=client_account.id, user_id=guardian.id,
            subscription_id=sub.id, stripe_invoice_id="in_audit1",
            amount_cents=19845, tax_cents=945, currency="CAD", status="paid",
            agency_share_cents=4725,
        )
    )
    db.session.commit()
    sent.clear()
    assert jobs.payment_health_check() == 0
    assert sent == []


def test_rebook_no_show_into_next_slot(app, client, client_account):
    """A no-show can be rebooked into the next occurrence of the same
    weekly slot, with a fresh confirmation and original attribution kept."""
    from app.models import Message

    instance = _first_instance(client_account, "kids_7_10")
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    booking.status = BookingStatus.no_show.value
    db.session.commit()
    guardian_id = booking.attendee.user_id
    lead_id = booking.lead_id
    before = (
        db.session.query(Message)
        .filter_by(template="booking_confirmation", channel="email")
        .count()
    )

    staff = _admin(app)
    r = staff.get(f"/ops/members/{guardian_id}")
    assert b"Rebook" in r.data

    # Default rebook (no instance chosen) → next occurrence of the same slot
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={"action": "rebook", "booking_id": str(booking.id)},
        follow_redirects=True,
    )
    assert b"rebooked into" in r.data

    booked = (
        db.session.query(Booking)
        .filter_by(attendee_id=booking.attendee_id, status=BookingStatus.booked.value)
        .all()
    )
    assert len(booked) == 1
    new = booked[0]
    assert new.class_instance_id != instance.id  # a future instance
    assert new.class_instance.template_id == instance.template_id  # same slot
    assert new.lead_id == lead_id  # attribution preserved

    # Selectable rebook: staff picks a specific upcoming instance
    pick_id = new.class_instance_id  # the slot they were just booked into
    new.status = BookingStatus.cancelled.value  # free them up again
    db.session.commit()
    pick = db.session.get(ClassInstance, pick_id)
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "rebook",
            "booking_id": str(booking.id),
            "instance_id": str(pick.id),
        },
        follow_redirects=True,
    )
    assert b"rebooked into" in r.data
    assert (
        db.session.query(Booking)
        .filter_by(attendee_id=booking.attendee_id, class_instance_id=pick.id,
                   status=BookingStatus.booked.value)
        .count()
        == 1
    )
    after = (
        db.session.query(Message)
        .filter_by(template="booking_confirmation", channel="email")
        .count()
    )
    assert after == before + 2  # a fresh confirmation for each rebook


def test_reschedule_upcoming_booking_releases_old_seat(app, client, client_account):
    """A still-upcoming booked class can be rescheduled to another class:
    the member moves, the original seat is released, one fresh confirmation."""
    from app.services.scheduling import upcoming_instances

    instance = _first_instance(client_account, "kids_7_10")
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    assert booking.status == BookingStatus.booked.value  # upcoming, live
    guardian_id = booking.attendee.user_id

    # pick a different upcoming instance of the same type
    with app.test_request_context():
        occ = upcoming_instances(client_account.id, class_type_id=instance.class_type_id)
    target = next(o["instance"] for o in occ if o["instance"].id != instance.id)

    staff = _admin(app)
    r = staff.get(f"/ops/members/{guardian_id}")
    assert b"Reschedule" in r.data
    r = staff.post(
        f"/ops/members/{guardian_id}",
        data={
            "action": "rebook",
            "booking_id": str(booking.id),
            "instance_id": str(target.id),
        },
        follow_redirects=True,
    )
    assert b"rescheduled to" in r.data

    db.session.refresh(booking)
    assert booking.status == BookingStatus.cancelled.value  # old seat released
    moved = (
        db.session.query(Booking)
        .filter_by(attendee_id=booking.attendee_id, class_instance_id=target.id)
        .one()
    )
    assert moved.status == BookingStatus.booked.value
    # exactly one live booking for this attendee
    assert (
        db.session.query(Booking)
        .filter_by(attendee_id=booking.attendee_id, status=BookingStatus.booked.value)
        .count()
        == 1
    )


def test_payment_link_creates_billed_membership(app, client, client_account):
    """Send-payment-link makes a member + attributed lead + payment-setup
    link; following it with a card on file starts a recurring membership
    (tracked, commissionable) — no class booking involved."""
    from app.models import (
        AttendeeProfile,
        Lead,
        Message,
        PaymentMethodStatus,
        StripeCustomer,
        Subscription,
        User,
    )
    from app.services.signed_links import SALT_MEMBERSHIP_SETUP, read_payload_token

    staff = _admin(app)
    r = staff.get("/ops/members")
    assert b"Send payment link" in r.data

    r = staff.post(
        "/ops/members/payment-link",
        data={"name": "Casey Caller", "email": "casey@example.com",
              "phone": "", "segment": "beast"},
        follow_redirects=True,
    )
    assert b"Payment link sent" in r.data
    guardian = db.session.query(User).filter_by(email="casey@example.com").one()
    att = db.session.query(AttendeeProfile).filter_by(user_id=guardian.id).one()
    assert att.kind == "self"
    lead = db.session.query(Lead).filter_by(user_id=guardian.id).one()
    assert lead.utm_source == "meta" and lead.utm_medium == "phone"
    assert lead.utm_campaign == "beast" and lead.utm_content == "call-in"

    msg = db.session.query(Message).filter_by(template="payment_link").one()
    token = re.search(r"/membership/start/([\w.\-]+)", msg.body_preview).group(1)
    assert read_payload_token(token, SALT_MEMBERSHIP_SETUP)["aid"] == att.id

    # No subscription until a card is on file
    assert db.session.query(Subscription).filter_by(user_id=guardian.id).count() == 0

    # Simulate the member having saved a card (Stripe unconfigured in tests,
    # so activation creates the local subscription without live Stripe calls).
    db.session.add(
        StripeCustomer(
            user_id=guardian.id, stripe_customer_id="cus_pl",
            payment_method_status=PaymentMethodStatus.vaulted.value,
        )
    )
    db.session.commit()

    visitor = app.test_client()
    r = visitor.get(f"/membership/start/{token}", follow_redirects=True)
    assert b"all set" in r.data.lower()
    subs = db.session.query(Subscription).filter_by(user_id=guardian.id).all()
    assert len(subs) == 1 and subs[0].attendee_id == att.id
    assert subs[0].mrr_cents == 18900  # Gold plan, billed through us

    # Idempotent: revisiting doesn't double-subscribe
    visitor.get(f"/membership/start/{token}", follow_redirects=True)
    assert db.session.query(Subscription).filter_by(user_id=guardian.id).count() == 1


def test_payments_page_failed_then_paid_and_reconcile(app, client, client_account):
    """Failed charges are recorded (past_due + a 'failed' payment row); a
    retry that succeeds promotes the same row to paid with commission; the
    admin Payments page and its CSV show it for reconciliation."""
    from app.models import Payment, Subscription

    guardian, child = _make_member(client_account, email="pay@example.com")
    sub = db.session.query(Subscription).filter_by(user_id=guardian.id).one()
    sub.stripe_subscription_id = "sub_pay1"
    db.session.commit()

    # a failed invoice → past_due + a 'failed' payment row (no commission)
    _webhook(client, "invoice.payment_failed", {
        "id": "in_pay1", "subscription": "sub_pay1",
        "amount_due": 19845, "tax": 945,
    })
    p = db.session.query(Payment).filter_by(stripe_invoice_id="in_pay1").one()
    assert p.status == "failed" and p.amount_cents == 19845
    assert p.agency_share_cents == 0
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.past_due.value

    # the retry succeeds → SAME row promoted to paid, commission booked
    _webhook(client, "invoice.paid", {
        "id": "in_pay1", "subscription": "sub_pay1",
        "amount_paid": 19845, "tax": 945, "charge": "ch_1",
    })
    assert db.session.query(Payment).filter_by(stripe_invoice_id="in_pay1").count() == 1
    db.session.refresh(p)
    assert p.status == "paid" and p.agency_share_cents == 4725

    # admin Payments page shows it; CSV exports for reconciliation
    staff = _admin(app)
    r = staff.get("/ops/payments")
    assert b"Payments" in r.data and b"pay@example.com" in r.data
    r = staff.get("/ops/payments?format=csv")
    assert b"your_25pct" in r.data and b"in_pay1" in r.data


def test_payment_link_from_payments_page_returns_there(app, client, client_account):
    """The payment-link tool on the Payments screen returns to Payments."""
    staff = _admin(app)
    r = staff.get("/ops/payments")
    assert b"Send a payment link" in r.data
    r = staff.post(
        "/ops/members/payment-link",
        data={"name": "Dana Dialer", "email": "dana@example.com",
              "phone": "", "segment": "", "return_to": "payments"},
        follow_redirects=True,
    )
    assert b"Payment link sent" in r.data
    # landed back on the Payments ledger, not the Members directory
    assert b"reconciling against Stripe" in r.data


def test_send_card_update_link_from_member_page(app, client, client_account):
    """A member phones the desk to change their card: one click on the member
    page emails (and texts) the same secure signed card page the dunning flow
    uses — no failed payment or password required, neutral wording."""
    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian = db.session.get(User, booking.attendee.user_id)

    staff = _admin(app)
    r = staff.get(f"/ops/members/{guardian.id}")
    assert b"Send card update link" in r.data

    r = staff.post(
        f"/ops/members/{guardian.id}",
        data={"action": "send_card_link"},
        follow_redirects=True,
    )
    assert b"Card update link sent" in r.data
    email = db.session.query(Message).filter_by(template="card_update", channel="email").one()
    assert "/portal/card/" in email.body_preview
    assert "didn't go through" not in email.body_preview  # not a dunning
    token = re.search(r"/portal/card/([\w\-\.]+)", email.body_preview).group(1)
    r = client.get(f"/portal/card/{token}")  # link works without login
    assert r.status_code == 200
    if guardian.phone:
        sms = db.session.query(Message).filter_by(template="card_update", channel="sms").one()
        assert "/portal/card/" in sms.body_preview


def test_new_card_becomes_charging_card_and_retries_past_due(
    app, client, client_account, monkeypatch
):
    """A replacement card must be the card Stripe CHARGES, not just a row on
    our side: subscriptions are pinned to the card they were created with,
    so a member who updated their card was still being retried on the old
    one (Sep 2026). Saving a card now sets it as the customer default and on
    every live subscription, and pays a past-due invoice on it immediately."""
    from app.services import stripe_service
    from app.services.signed_links import SALT_UPDATE_CARD, make_token

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian = booking.attendee.guardian
    sc = StripeCustomer(
        user_id=guardian.id, stripe_customer_id="cus_t2",
        stripe_payment_method_id="pm_old",
    )
    db.session.add(sc)
    from app.models import Plan
    plan = db.session.query(Plan).filter_by(client_account_id=client_account.id).first()
    db.session.add(Subscription(
        client_account_id=client_account.id, user_id=guardian.id,
        attendee_id=booking.attendee_id, plan_id=plan.id,
        stripe_subscription_id="sub_t2", status=SubscriptionStatus.past_due.value,
        mrr_cents=18900,
    ))
    db.session.commit()

    calls = []

    class FakeSI:
        status = "succeeded"
        customer = "cus_t2"
        payment_method = "pm_new"

    class FakeStripe:
        class SetupIntent:
            @staticmethod
            def retrieve(_id):
                return FakeSI()

        class Customer:
            @staticmethod
            def modify(cus, **kw):
                calls.append(("customer", cus, kw))

        class Subscription:
            @staticmethod
            def modify(sid, **kw):
                calls.append(("sub", sid, kw))

        class Invoice:
            @staticmethod
            def list(**kw):
                class R:
                    data = [type("Inv", (), {"id": "in_open_1"})()]
                return R()

            @staticmethod
            def pay(inv_id, **kw):
                calls.append(("pay", inv_id, kw))
                return {"status": "paid"}

    monkeypatch.setattr(stripe_service, "is_configured", lambda: True)
    monkeypatch.setattr(stripe_service, "stripe_client", lambda: FakeStripe)

    with app.test_request_context():
        tok = make_token(guardian.id, SALT_UPDATE_CARD)
    r = client.get(f"/portal/card/{tok}?setup_intent=seti_2&redirect_status=succeeded")
    assert r.status_code == 200
    db.session.refresh(sc)
    assert sc.stripe_payment_method_id == "pm_new"
    assert ("customer", "cus_t2", {"invoice_settings": {"default_payment_method": "pm_new"}}) in calls
    assert ("sub", "sub_t2", {"default_payment_method": "pm_new"}) in calls
    assert ("pay", "in_open_1", {"payment_method": "pm_new"}) in calls


def test_member_page_shows_card_on_file_live_from_stripe(
    app, client, client_account, monkeypatch
):
    """"Did my card go through?" is answerable on the member page: every
    card Stripe holds, which one it will charge, and a failed payment
    awaiting retry — live, not our cached status."""
    import json

    from app.models import Plan
    from app.services import stripe_service

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian = booking.attendee.guardian
    db.session.add(StripeCustomer(
        user_id=guardian.id, stripe_customer_id="cus_t3",
        stripe_payment_method_id="pm_new",
    ))
    plan = db.session.query(Plan).filter_by(client_account_id=client_account.id).first()
    db.session.add(Subscription(
        client_account_id=client_account.id, user_id=guardian.id,
        attendee_id=booking.attendee_id, plan_id=plan.id,
        stripe_subscription_id="sub_t3", status=SubscriptionStatus.past_due.value,
        mrr_cents=18900,
    ))
    db.session.commit()

    class J(dict):  # str() -> JSON, like a StripeObject
        def __str__(self):
            return json.dumps(self)

    class FakeStripe:
        class PaymentMethod:
            @staticmethod
            def list(**kw):
                return J(data=[
                    {"id": "pm_old", "created": 100, "card": {"brand": "mastercard", "last4": "6210", "exp_month": 4, "exp_year": 2030}},
                    {"id": "pm_new", "created": 200, "card": {"brand": "visa", "last4": "6118", "exp_month": 8, "exp_year": 2031}},
                ])

        class Customer:
            @staticmethod
            def retrieve(_id):
                return J(invoice_settings={"default_payment_method": "pm_new"})

        class Subscription:
            @staticmethod
            def retrieve(_id, **kw):
                return J(status="past_due", default_payment_method="pm_new", current_period_end=1792000000)

        class Invoice:
            @staticmethod
            def list(**kw):
                return J(data=[{"id": "in_open", "amount_due": 19845, "attempt_count": 1, "next_payment_attempt": 1790000000}])

    monkeypatch.setattr(stripe_service, "is_configured", lambda: True)
    monkeypatch.setattr(stripe_service, "stripe_client", lambda: FakeStripe)

    r = _admin(app).get(f"/ops/members/{guardian.id}")
    html = r.data.decode()
    assert "Card on file" in html
    assert "6118" in html and "6210" in html
    # the chip sits on the new Visa, not the old Mastercard
    assert html.index("6118") < html.index("charges this card") < html.index("6210")
    assert "$198.45 payment failed" in html
    assert "Stripe retries it" in html


def test_end_membership_now_for_refund_case(app, client, client_account):
    """A goodwill/refund cancellation must end the membership immediately with
    no 30-day notice, so no further charge follows the refund. The default
    staff cancel keeps the terms' notice period."""
    from app.models import Plan
    from app.services import billing

    instance = _first_instance(client_account)
    _book_child(client, instance)
    booking = db.session.query(Booking).one()
    guardian = booking.attendee.guardian
    plan = db.session.query(Plan).filter_by(client_account_id=client_account.id).first()
    sub = Subscription(
        client_account_id=client_account.id, user_id=guardian.id,
        attendee_id=booking.attendee_id, plan_id=plan.id,
        status=SubscriptionStatus.active.value, mrr_cents=18900,
        activated_at=utcnow(),
    )
    db.session.add(sub); db.session.commit()

    staff = _admin(app)
    r = staff.post(f"/ops/members/{guardian.id}", data={"action": "cancel_sub_now", "sub_id": str(sub.id)}, follow_redirects=True)
    assert b"ended now" in r.data
    db.session.refresh(sub)
    assert sub.status == SubscriptionStatus.cancelled.value
    assert sub.cancelled_at is not None
    assert sub.cancel_requested_at is None  # no notice period recorded
    assert sub.cancel_reason == "goodwill"

    # default path on another activated sub: notice, not immediate
    sub2 = Subscription(
        client_account_id=client_account.id, user_id=guardian.id,
        attendee_id=booking.attendee_id, plan_id=plan.id,
        status=SubscriptionStatus.active.value, mrr_cents=18900, activated_at=utcnow(),
    )
    db.session.add(sub2); db.session.commit()
    billing.cancel_subscription(sub2, reason="staff_initiated")
    assert sub2.status == SubscriptionStatus.active.value
    assert sub2.cancel_requested_at is not None and sub2.cancel_effective_at is not None
