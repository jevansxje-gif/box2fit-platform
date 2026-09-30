"""Move a weekly class to a new start time (all weekdays it runs on). Future
sessions move with their bookings; booked members get the schedule-change
email. Same as Ops -> Schedule -> "Every <day> ->", for every day at once.

    .venv/bin/python -m scripts.move_class she_hits 09:00
"""
import sys
from datetime import time

from app import create_app
from app.extensions import db
from app.models import ClassType, ScheduleTemplate
from app.services.class_admin import reschedule_template

if len(sys.argv) < 3:
    raise SystemExit("usage: -m scripts.move_class <class_type_key> <HH:MM>")
key, hhmm = sys.argv[1], sys.argv[2]
h, m = (int(x) for x in hhmm.split(":"))
app = create_app()
with app.app_context():
    ct = db.session.query(ClassType).filter_by(key=key).one()
    tpls = db.session.query(ScheduleTemplate).filter_by(class_type_id=ct.id, active=True).all()
    moved = 0
    for t in tpls:
        if t.start_time_local != time(h, m):
            moved += reschedule_template(t, t.weekday, time(h, m))
    db.session.commit()
    print(f"{ct.name}: {len(tpls)} weekly template(s) now at {hhmm}; {moved} future session(s) moved")
