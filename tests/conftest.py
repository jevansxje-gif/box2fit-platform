import sys
from datetime import time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
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
from app.services.scheduling import generate_instances  # noqa: E402
from app.services.tzutil import today_local  # noqa: E402


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        _seed()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def client_account(app):
    return db.session.query(ClientAccount).one()


def _seed():
    ca = ClientAccount(name="Box2Fit", slug="box2fit", commission_rate=0.25)
    db.session.add(ca)
    db.session.flush()

    trainer = Trainer(
        client_account_id=ca.id,
        name="Coach Alex T.",
        certifications=["NCCP Boxing"],
        active=True,
    )
    db.session.add(trainer)
    db.session.flush()

    kids = ClassType(
        client_account_id=ca.id,
        key="kids_7_10",
        name="Kids Boxing",
        segment_tag="youth",
        age_min=7,
        age_max=10,
        duration_min=45,
        default_capacity=2,
    )
    teens = ClassType(
        client_account_id=ca.id,
        key="teen_15_17",
        name="Teen Boxing",
        segment_tag="youth",
        age_min=15,
        age_max=17,
        duration_min=45,
        default_capacity=12,
    )
    db.session.add_all([kids, teens])
    db.session.flush()

    # One weekly template per type on tomorrow's weekday, late in the day so
    # generated instances are always in the future during the test run.
    tomorrow = today_local() + timedelta(days=1)
    for ct, cohort in ((kids, "Group A"), (teens, "Group B")):
        db.session.add(
            ScheduleTemplate(
                client_account_id=ca.id,
                class_type_id=ct.id,
                cohort_label=cohort,
                weekday=tomorrow.weekday(),
                start_time_local=time(11, 0),
                trainer_id=trainer.id,
                active=True,
            )
        )
    db.session.add(
        Plan(
            client_account_id=ca.id,
            name="Unlimited",
            price_cents=18900,
            is_placeholder=True,
        )
    )
    db.session.add(
        WaiverDocument(
            client_account_id=ca.id, kind="minor", version=1, body_md="waiver v1"
        )
    )
    staff = User(
        client_account_id=ca.id,
        email="frontdesk@test.local",
        name="Front Desk",
        role=Role.front_desk.value,
    )
    staff.set_password("pw")
    db.session.add(staff)
    db.session.add(
        Review(
            client_account_id=ca.id,
            reviewer_name="Michelle A.",
            rating=5,
            quote_text="My son loves the BOX2FIT kids class!",
            segment_tags="youth,reset",
        )
    )
    SiteSetting.set("google_rating", "5.0")
    SiteSetting.set("google_review_count", "28")
    db.session.commit()

    generate_instances(ca.id, weeks=2)
    db.session.commit()
