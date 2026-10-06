"""Give a class a launch date: nothing is bookable before it, and the empty
sessions already on the calendar before that date are removed. Sessions that
already have a booking are left alone and listed, so nobody is cancelled by
accident.

    .venv/bin/python -m scripts.set_start beast 2026-10-26
"""
import sys
from datetime import date

from app import create_app
from app.extensions import db
from app.models import BookingStatus, ClassInstance, ClassType, ScheduleTemplate
from app.services.tzutil import today_local

if len(sys.argv) < 3:
    raise SystemExit("usage: -m scripts.set_start <class_type_key> <YYYY-MM-DD>")
key, launch = sys.argv[1], date.fromisoformat(sys.argv[2])
app = create_app()
with app.app_context():
    ct = db.session.query(ClassType).filter_by(key=key).one()
    tpls = db.session.query(ScheduleTemplate).filter_by(class_type_id=ct.id, active=True).all()
    for t in tpls:
        t.starts_on = launch
    tids = [t.id for t in tpls]
    early = (db.session.query(ClassInstance)
             .filter(ClassInstance.template_id.in_(tids),
                     ClassInstance.local_date >= today_local(),
                     ClassInstance.local_date < launch).all())
    removed, kept = 0, []
    for inst in early:
        if any(b.status == BookingStatus.booked.value for b in inst.bookings):
            kept.append(inst.local_date)
        elif inst.bookings:
            inst.status = "cancelled"; removed += 1
        else:
            db.session.delete(inst); removed += 1
    db.session.commit()
    print(f"{ct.name}: {len(tpls)} weekly slot(s) now launch {launch}; {removed} empty session(s) before then removed")
    if kept:
        print(f"   kept {len(kept)} session(s) that already have a booking: {', '.join(str(d) for d in sorted(kept))}")
