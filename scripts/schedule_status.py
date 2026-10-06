"""Read-only: has each class actually started, and what is booked ahead?

Per class type (and kids group): the weekly slots, any launch date set on
them, the first session anyone checked in to, how many sessions have had a
check-in, and the booked sessions coming up.

    .venv/bin/python -m scripts.schedule_status
"""
from collections import defaultdict

from app import create_app
from app.extensions import db
from app.models import Booking, BookingStatus, ClassInstance, ClassType, ScheduleTemplate
from app.services.tzutil import today_local

DAYS = "Mon Tue Wed Thu Fri Sat Sun".split()
app = create_app()
with app.app_context():
    today = today_local()
    types = db.session.query(ClassType).order_by(ClassType.id).all()
    for ct in types:
        tpls = db.session.query(ScheduleTemplate).filter_by(class_type_id=ct.id).all()
        if not tpls:
            continue
        print(f"\n== {ct.name} ({ct.key})")
        groups = defaultdict(list)
        for t in tpls:
            groups[(t.cohort_label or "", t.active)].append(t)
        for (cohort, active), ts in sorted(groups.items()):
            ts.sort(key=lambda t: (t.weekday, str(t.start_time_local)))
            days = "/".join(DAYS[t.weekday] for t in ts)
            times = sorted({t.start_time_local.strftime("%H:%M") for t in ts})
            starts = sorted({str(t.starts_on) for t in ts if t.starts_on})
            print(f"   slots {cohort or '-':8s} {days} at {', '.join(times)}"
                  f"{'' if active else '  [INACTIVE]'}{'  launch ' + ', '.join(starts) if starts else '  (no launch date)'}")
            tids = [t.id for t in ts]
            inst = (db.session.query(ClassInstance)
                    .filter(ClassInstance.template_id.in_(tids)).all())
            past = [i for i in inst if i.local_date < today]
            future = sorted((i for i in inst if i.local_date >= today and i.status == "scheduled"), key=lambda i: i.local_date)
            attended = []
            for i in past:
                if any(b.checked_in_at for b in i.bookings):
                    attended.append(i.local_date)
            booked_future = [(i.local_date, sum(1 for b in i.bookings if b.status == BookingStatus.booked.value)) for i in future]
            booked_future = [(d, n) for d, n in booked_future if n]
            if attended:
                print(f"   started: first check-in {min(attended)}, {len(attended)} session(s) with a check-in so far")
            else:
                print(f"   NOT started: no session has had a check-in ({len(past)} past session(s) on the calendar)")
            if future:
                print(f"   upcoming: {len(future)} session(s) from {future[0].local_date}; "
                      f"{sum(n for _, n in booked_future)} booking(s) on {len(booked_future)} of them"
                      + (f": " + ", ".join(f"{d} x{n}" for d, n in booked_future[:8]) if booked_future else ""))
            else:
                print("   upcoming: none on the calendar")
    print(f"\n(today {today})")
