"""Set a class's length, on the class itself, its weekly slots and every
session from today on (calendar invites and no-show timing read it from the
session).

    .venv/bin/python -m scripts.set_duration beast 60
"""
import sys

from app import create_app
from app.extensions import db
from app.models import ClassInstance, ClassType, ScheduleTemplate
from app.services.tzutil import today_local

if len(sys.argv) < 3:
    raise SystemExit("usage: -m scripts.set_duration <class_type_key> <minutes>")
key, minutes = sys.argv[1], int(sys.argv[2])
app = create_app()
with app.app_context():
    ct = db.session.query(ClassType).filter_by(key=key).one()
    ct.duration_min = minutes
    tpls = db.session.query(ScheduleTemplate).filter_by(class_type_id=ct.id).all()
    for t in tpls:
        if t.duration_min:
            t.duration_min = minutes
    insts = (db.session.query(ClassInstance)
             .filter(ClassInstance.class_type_id == ct.id,
                     ClassInstance.local_date >= today_local()).all())
    for i in insts:
        i.duration_min = minutes
    db.session.commit()
    print(f"{ct.name}: {minutes} minutes; {len(tpls)} weekly slot(s), {len(insts)} session(s) from today updated")
