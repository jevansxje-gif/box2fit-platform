"""Remove the three seeded placeholder coaches (and ONLY them), unassigning
them from templates and future classes first. Real trainers added via ops are
untouched. Idempotent.

    python -m scripts.remove_placeholder_trainers
"""
from app import create_app
from app.extensions import db
from app.models import ClassInstance, ScheduleTemplate, Trainer

PLACEHOLDERS = ["Coach Alex T.", "Coach Marcus L.", "Coach Priya S."]

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        removed = 0
        for name in PLACEHOLDERS:
            t = db.session.query(Trainer).filter_by(name=name).one_or_none()
            if t is None:
                continue
            for tpl in db.session.query(ScheduleTemplate).filter_by(trainer_id=t.id):
                tpl.trainer_id = None
            for inst in db.session.query(ClassInstance).filter_by(trainer_id=t.id):
                inst.trainer_id = None
            db.session.delete(t)
            removed += 1
            print(f"removed: {name}")
        db.session.commit()
        remaining = db.session.query(Trainer).all()
        print(f"placeholders removed: {removed}")
        print("remaining trainers:", [t.name for t in remaining] or "none")
