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
# CLIENT SCHEDULE 2026-08-20. Kids keeps cohort GROUPS (client-confirmed
# 2026-07-24): enroll in a group (3 sessions/week for 4 weeks), RSVP per
# session. Youth Confidence is the 11-18 program (its own class + landing).
# key, name, segment, age_min, age_max, duration, capacity
CLASS_TYPES = [
    ("kids", "Kids Boxing", "kids", 6, 10, 45, 12),
    ("youth", "Youth Confidence", "youth", 11, 18, 45, 14),
    ("technical", "Technical Boxing", "technical", None, None, 60, 16),
    ("bootcamp", "Boxing Bootcamp", "bootcamp", None, None, 45, 16),
    ("she_hits", "She Hits", "shehits", None, None, 60, 16),
    ("beast", "Beast Camp", "beast", None, None, 50, 16),
]

# Retired catalog entries: deleted on reseed IF nothing references them.
# (reset/active keep their rows where history exists — their templates are
# deactivated by the schedule reconcile below.)
RETIRED_TYPE_KEYS = ["kids_7_10", "youth_11_14", "teen_15_17", "reset", "active"]

# ---- weekly schedule (weekday 0=Mon, local time, cohort) -------------------
# THE REAL SCHEDULE (client, 2026-08-20):
#   Kids: Group A Mon/Wed/Fri 4pm · Group B Tue/Thu 4pm + Sat 9am
#   Mon-Fri daily: 6am Beast · 10am She Hits · 5pm Bootcamp · 6pm Technical
#                  · 7pm Youth Confidence
# Bump SCHEDULE_VERSION whenever this table changes — the reconcile runs
# once per version so Builder edits made between versions are respected.
SCHEDULE_VERSION = "2026-08-20.2"  # .2: Saturday kids moved 4pm -> 9am
SCHEDULE = [
    ("kids", 0, time(16, 0), "Group A"),
    ("kids", 2, time(16, 0), "Group A"),
    ("kids", 4, time(16, 0), "Group A"),
    ("kids", 1, time(16, 0), "Group B"),
    ("kids", 3, time(16, 0), "Group B"),
    ("kids", 5, time(9, 0), "Group B"),
]
for _wd in range(5):  # Mon-Fri
    SCHEDULE += [
        ("beast", _wd, time(6, 0), None),
        ("she_hits", _wd, time(10, 0), None),
        ("bootcamp", _wd, time(17, 0), None),
        ("technical", _wd, time(18, 0), None),
        ("youth", _wd, time(19, 0), None),
    ]

TRAINERS = [  # PLACEHOLDER roster; certifications field populated per brief
    ("Coach Alex T.", "Youth & Foundations", ["NCCP Boxing", "High Five", "CPR/AED"]),
    ("Coach Marcus L.", "Strength & Conditioning", ["CSCS", "FMS L1", "CPR/AED"]),
    ("Coach Priya S.", "She Hits Lead", ["NCCP Boxing", "Pre/Postnatal", "CPR/AED"]),
]

AUDIENCE_TO_TAGS = {
    "active-adults": "technical,bootcamp",
    "parents": "kids,youth",
    "professionals": "bootcamp,technical",
    "women": "shehits",
    "teens": "youth",
    "beast-mode": "beast",
}

from app.legal import WAIVER_FULL_TEXT, WAIVER_VERSION  # client-supplied text


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

    # One-time rename (2026-08-20 program split): the original key "youth"
    # WAS the kids class. Move it to "kids" so the new 11-18 Youth Confidence
    # type can own the "youth" key. Idempotent: once "kids" exists, any
    # "youth" row is the new type and must not be touched.
    kids_row = db.session.query(ClassType).filter_by(
        client_account_id=client.id, key="kids"
    ).one_or_none()
    old_youth = db.session.query(ClassType).filter_by(
        client_account_id=client.id, key="youth"
    ).one_or_none()
    if kids_row is None and old_youth is not None:
        old_youth.key = "kids"
        old_youth.name = "Kids Boxing"
        db.session.flush()
        print("renamed class type: youth -> kids (Kids Boxing)")

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
            # keep existing rows aligned with the map (this table is the
            # source of truth for the catalog — Builder edits to these
            # fields are reverted on reseed)
            ct.segment_tag = seg
            ct.age_min = amin
            ct.age_max = amax
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

    # Retired catalog entries: delete only when nothing references them.
    from app.models import ClassInstance

    for rkey in RETIRED_TYPE_KEYS:
        ct = db.session.query(ClassType).filter_by(
            client_account_id=client.id, key=rkey
        ).one_or_none()
        if ct is None:
            continue
        refs = (
            db.session.query(ScheduleTemplate).filter_by(class_type_id=ct.id).count()
            + db.session.query(ClassInstance).filter_by(class_type_id=ct.id).count()
        )
        if refs == 0:
            db.session.delete(ct)
            print(f"retired class type deleted: {rkey}")

    # Schedule reconcile — runs ONCE per SCHEDULE_VERSION so day-to-day
    # Builder edits aren't clobbered by later reseeds. Templates matching the
    # map are (re)activated, missing ones created (Coach TBA — assign via
    # Trainers page), and everything else is deactivated with its future
    # bookingless instances pruned.
    if SiteSetting.get("schedule_version", "") != SCHEDULE_VERSION:
        from app.services.class_admin import (
            prune_inactive_template_instances,
            reschedule_template,
        )

        wanted = {
            (types[key].id, weekday, t_local, cohort)
            for key, weekday, t_local, cohort in SCHEDULE
        }
        seen = set()
        leftovers = []
        for tpl in db.session.query(ScheduleTemplate).filter_by(
            client_account_id=client.id
        ):
            sig = (tpl.class_type_id, tpl.weekday, tpl.start_time_local,
                   tpl.cohort_label)
            if sig in wanted:
                tpl.active = True
                seen.add(sig)
            else:
                leftovers.append(tpl)
        # A leftover matching a missing slot on everything but the time is a
        # TIME CHANGE: move it (bookings follow, members get the schedule-
        # change email) rather than deactivate-and-recreate.
        n_moved = 0
        to_add = wanted - seen
        for sig in sorted(to_add, key=lambda s: (s[0], s[1], str(s[2]), s[3] or "")):
            ct_id, weekday, t_local, cohort = sig
            match = next(
                (t for t in leftovers
                 if t.class_type_id == ct_id and t.weekday == weekday
                 and t.cohort_label == cohort),
                None,
            )
            if match is not None:
                reschedule_template(match, weekday, t_local)
                match.active = True
                leftovers.remove(match)
                to_add.discard(sig)
                n_moved += 1
        n_off = 0
        for tpl in leftovers:
            if tpl.active:
                tpl.active = False
                n_off += 1
        for sig in to_add:
            ct_id, weekday, t_local, cohort = sig
            db.session.add(
                ScheduleTemplate(
                    client_account_id=client.id,
                    class_type_id=ct_id,
                    cohort_label=cohort,
                    weekday=weekday,
                    start_time_local=t_local,
                    trainer_id=None,
                    active=True,
                )
            )
        db.session.flush()
        pruned = prune_inactive_template_instances(client.id)
        SiteSetting.set("schedule_version", SCHEDULE_VERSION)
        print(
            f"schedule reconciled to {SCHEDULE_VERSION}: {len(to_add)} added, "
            f"{n_moved} moved, {n_off} deactivated, {pruned} pruned"
        )

    for kind in ("minor", "adult"):
        exists = db.session.query(WaiverDocument).filter_by(
            client_account_id=client.id, kind=kind, version=WAIVER_VERSION
        ).first()
        if not exists:
            db.session.add(
                WaiverDocument(
                    client_account_id=client.id,
                    kind=kind,
                    version=WAIVER_VERSION,
                    body_md=WAIVER_FULL_TEXT,
                )
            )
    print(f"waiver documents: minor + adult v{WAIVER_VERSION} (client legal text)")

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
            if existing is not None:
                existing.segment_tags = tags  # re-tag when the map changes
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
