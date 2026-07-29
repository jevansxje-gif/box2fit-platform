"""Pass 1 seed — per the kickoff brief. All schedule times, trainer names and
prices are PLACEHOLDERS, clearly swappable here. Idempotent.

    python -m scripts.seed
"""
import json
from datetime import time
from pathlib import Path

from app import create_app
from app.extensions import db
from app.models import (
    ClassType,
    ClientAccount,
    Plan,
    Review,
    Role,
    ScheduleTemplate,
    SiteSetting,
    Trainer,
    User,
    WaiverDocument,
)
from app.services.scheduling import generate_instances

ROOT = Path(__file__).resolve().parent.parent

# ---- class types --------------------------------------------------------
# Youth launches with cohort GROUPS (client-confirmed 2026-07-24): members
# enroll in a group (3 sessions/week for 4 weeks) and RSVP per session.
# The bracket types (7-10/11-14/15-17) stay in the catalog, unscheduled,
# ready for when groups split by age.
# key, name, segment, age_min, age_max, duration, capacity
CLASS_TYPES = [
    ("youth", "Youth Boxing", "youth", 7, 17, 45, 12),
    ("kids_7_10", "Kids Boxing", "youth", 7, 10, 45, 12),
    ("youth_11_14", "Youth Boxing 11-14", "youth", 11, 14, 45, 12),
    ("teen_15_17", "Teen Boxing", "youth", 15, 17, 45, 12),
    # segment 'focus': the professionals landing (/focus) books these midday
    # classes; the /reset parents landing routes into the youth flow instead.
    ("reset", "Reset Midday Boxing", "focus", None, None, 45, 16),
    ("she_hits", "She Hits", "shehits", None, None, 60, 16),
    ("beast", "Beast Camp", "beast", None, None, 50, 16),
    ("active", "Active Adults Boxing", "strong", None, None, 45, 16),
]

# ---- weekly schedule (weekday 0=Mon, local time, trainer idx, cohort) ------
# THE REAL STARTING SCHEDULE (client, 2026-07-24) — expand by adding rows:
#   Group A: Mon/Wed/Fri 4pm · Group B: Tue/Thu/Sat 4pm
SCHEDULE = [
    ("youth", 0, time(16, 0), 0, "Group A"),
    ("youth", 2, time(16, 0), 0, "Group A"),
    ("youth", 4, time(16, 0), 0, "Group A"),
    ("youth", 1, time(16, 0), 0, "Group B"),
    ("youth", 3, time(16, 0), 0, "Group B"),
    ("youth", 5, time(16, 0), 0, "Group B"),
    # Secondary adult slots (kickoff placeholders, unchanged)
    ("reset", 1, time(12, 0), 1, None),
    ("reset", 3, time(12, 0), 1, None),
    ("she_hits", 1, time(18, 0), 2, None),
    ("she_hits", 3, time(18, 0), 2, None),
    ("beast", 0, time(6, 0), 1, None),
    ("beast", 2, time(6, 0), 1, None),
    ("beast", 4, time(6, 0), 1, None),
    ("beast", 0, time(19, 0), 1, None),
    ("beast", 2, time(19, 0), 1, None),
    ("beast", 4, time(19, 0), 1, None),
    ("active", 0, time(10, 0), 2, None),
    ("active", 2, time(10, 0), 2, None),
    ("active", 4, time(10, 0), 2, None),
]

TRAINERS = [  # PLACEHOLDER roster; certifications field populated per brief
    ("Coach Alex T.", "Youth & Foundations", ["NCCP Boxing", "High Five", "CPR/AED"]),
    ("Coach Marcus L.", "Strength & Conditioning", ["CSCS", "FMS L1", "CPR/AED"]),
    ("Coach Priya S.", "She Hits Lead", ["NCCP Boxing", "Pre/Postnatal", "CPR/AED"]),
]

AUDIENCE_TO_TAGS = {
    "active-adults": "strong",
    "parents": "reset,youth",
    "professionals": "focus",
    "women": "shehits",
    "teens": "youth",
    "beast-mode": "beast",
}

MINOR_WAIVER_MD = (
    "**Youth Participation Waiver (v1 placeholder).** As parent or legal "
    "guardian, I consent to the named child's participation in boxing and "
    "fitness activities at Box2Fit, understand the inherent risks, and "
    "release Box2Fit, its coaches and staff from liability for injuries "
    "arising from ordinary negligence, to the extent permitted by BC law. "
    "Full legal text to be supplied before launch."
)
ADULT_WAIVER_MD = MINOR_WAIVER_MD.replace(
    "As parent or legal guardian, I consent to the named child's", "I consent to my"
)


def seed():
    client = db.session.query(ClientAccount).filter_by(slug="box2fit").one_or_none()
    if client is None:
        client = ClientAccount(name="Box2Fit", slug="box2fit", commission_rate=0.25)
        db.session.add(client)
        db.session.flush()
    print(f"client account: {client.name} (commission {client.commission_rate})")

    # Placeholder coaches seed ONLY into an empty roster — once real trainers
    # exist (added via ops), re-seeding never resurrects the placeholders.
    trainers = db.session.query(Trainer).filter_by(client_account_id=client.id).all()
    if not trainers:
        for name, role_title, certs in TRAINERS:
            t = Trainer(
                client_account_id=client.id,
                name=name,
                role_title=role_title,
                certifications=certs,
                active=True,
            )
            db.session.add(t)
            db.session.flush()
            trainers.append(t)
        print(f"trainers: {len(trainers)} placeholders (empty roster)")
    else:
        print(f"trainers: {len(trainers)} existing, none added")

    types = {}
    for key, name, seg, amin, amax, dur, cap in CLASS_TYPES:
        ct = db.session.query(ClassType).filter_by(
            client_account_id=client.id, key=key
        ).one_or_none()
        if ct is None:
            ct = ClassType(
                client_account_id=client.id,
                key=key,
                name=name,
                segment_tag=seg,
                age_min=amin,
                age_max=amax,
                duration_min=dur,
                default_capacity=cap,
                accepts_trials=True,
            )
            db.session.add(ct)
            db.session.flush()
        else:
            ct.segment_tag = seg  # keep existing rows aligned with the map
        types[key] = ct
    print(f"class types: {len(types)}")

    # Membership: $189 per 4-WEEK CYCLE, auto-renewing — CONFIRMED 2026-07-24.
    # Structure: 3 sessions/week within the member's group (no group swaps),
    # RSVP per session. Stripe (Pass 2): interval=week, interval_count=4.
    plan = db.session.query(Plan).filter_by(client_account_id=client.id).first()
    if plan is None:
        plan = Plan(
            client_account_id=client.id,
            class_type_id=None,
            name="Membership",
            price_cents=18900,
        )
        db.session.add(plan)
    plan.price_cents = 18900
    plan.interval = "4_weeks"
    plan.is_placeholder = False
    print("plans: Membership $189 / 4-week cycle (confirmed)")

    if (
        db.session.query(ScheduleTemplate)
        .filter_by(client_account_id=client.id)
        .count()
        == 0
    ):
        for key, weekday, t_local, trainer_idx, cohort in SCHEDULE:
            db.session.add(
                ScheduleTemplate(
                    client_account_id=client.id,
                    class_type_id=types[key].id,
                    cohort_label=cohort,
                    weekday=weekday,
                    start_time_local=t_local,
                    trainer_id=trainers[trainer_idx].id
                    if trainer_idx < len(trainers)
                    else None,
                    active=True,
                )
            )
        print(f"schedule templates: {len(SCHEDULE)}")

    for kind, body in (("minor", MINOR_WAIVER_MD), ("adult", ADULT_WAIVER_MD)):
        exists = db.session.query(WaiverDocument).filter_by(
            client_account_id=client.id, kind=kind, active=True
        ).first()
        if not exists:
            db.session.add(
                WaiverDocument(
                    client_account_id=client.id, kind=kind, version=1, body_md=body
                )
            )
    print("waiver documents: minor + adult v1")

    # Ops staff (dev credentials — change on staging)
    for email, name, role, pw in (
        ("frontdesk@box2fit.local", "Front Desk", Role.front_desk.value, "box2fit-dev"),
        ("admin@box2fit.local", "Gym Admin", Role.gym_admin.value, "box2fit-dev"),
    ):
        u = db.session.query(User).filter_by(
            client_account_id=client.id, email=email
        ).one_or_none()
        if u is None:
            u = User(
                client_account_id=client.id, email=email, name=name, role=role
            )
            u.set_password(pw)
            db.session.add(u)
    print("staff users: frontdesk@box2fit.local / admin@box2fit.local (pw: box2fit-dev)")

    # Verified Google reviews (verbatim) + aggregate badge
    reviews_path = ROOT / "content" / "reviews.json"
    if reviews_path.exists():
        data = json.loads(reviews_path.read_text(encoding="utf-8"))
        SiteSetting.set("google_rating", str(data["business"]["rating"]))
        SiteSetting.set("google_review_count", str(data["business"]["count"]))
        for i, r in enumerate(data["reviews"]):
            existing = db.session.query(Review).filter_by(
                client_account_id=client.id, reviewer_name=r["name"]
            ).one_or_none()
            tags = ",".join(AUDIENCE_TO_TAGS[a] for a in r["audience"])
            if existing is None:
                db.session.add(
                    Review(
                        client_account_id=client.id,
                        reviewer_name=r["name"],
                        rating=5,
                        quote_text=r["quote"],
                        segment_tags=tags,
                        display_order=i,
                    )
                )
        print(f"reviews: {len(data['reviews'])} (verbatim, verified profile)")

    db.session.commit()

    created = generate_instances(client.id)
    db.session.commit()
    print(f"class instances generated: {created}")
    print("seed complete")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        seed()
