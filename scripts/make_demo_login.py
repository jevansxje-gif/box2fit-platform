"""Create (or reset) a DEMO member login that mirrors a real post-signup
parent — guardian account + child + a booked upcoming class + a pending
membership (first charge as arranged) + a card marked on file. Lets staff
log in and see exactly what a parent sees, to guide them.

    .venv/bin/python -m scripts.make_demo_login [PASSWORD]
    .venv/bin/python -m scripts.make_demo_login --remove   # delete the demo

The demo guardian/child are named "Demo ..." so they're obvious in any
roster. Safe to re-run; it resets the demo each time.
"""
import sys
from datetime import date, timedelta

from app import create_app
from app.extensions import db
from app.models import (
    AttendeeKind,
    AttendeeProfile,
    Booking,
    BookingKind,
    BookingStatus,
    ClassInstance,
    ClassType,
    ClientAccount,
    InstanceStatus,
    PaymentMethodStatus,
    Role,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services.billing import default_plan
from app.services.tzutil import local_to_utc, now_utc

DEMO_EMAIL = "demo.parent@box2fit.local"
app = create_app()


def _remove():
    u = db.session.query(User).filter_by(email=DEMO_EMAIL).one_or_none()
    if not u:
        print("No demo account to remove.")
        return
    atts = db.session.query(AttendeeProfile).filter_by(user_id=u.id).all()
    ids = [a.id for a in atts]
    if ids:
        db.session.query(Booking).filter(Booking.attendee_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.session.query(Subscription).filter(
            Subscription.attendee_id.in_(ids)
        ).delete(synchronize_session=False)
    for a in atts:
        db.session.delete(a)
    db.session.query(StripeCustomer).filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    print("Demo account removed.")


with app.app_context():
    if "--remove" in sys.argv:
        _remove()
        raise SystemExit(0)

    password = next((a for a in sys.argv[1:] if not a.startswith("--")), "Box2Fit-Guide-9")
    _remove()  # reset first so re-runs stay clean

    ca = db.session.query(ClientAccount).filter_by(active=True).first()

    guardian = User(
        client_account_id=ca.id,
        email=DEMO_EMAIL,
        name="Demo Parent",
        phone="+16040000000",
        role=Role.member.value,
        consent_email=True,
    )
    guardian.set_password(password)
    db.session.add(guardian)
    db.session.flush()

    child = AttendeeProfile(
        client_account_id=ca.id,
        user_id=guardian.id,
        kind=AttendeeKind.child.value,
        first_name="Demo",
        last_name="Kid",
        birth_year=date.today().year - 8,
    )
    db.session.add(child)
    db.session.flush()

    # A card on file (placeholder — payment-management pages will try Stripe
    # and error, but the dashboard/schedule the parent uses do not).
    db.session.add(
        StripeCustomer(
            user_id=guardian.id,
            stripe_customer_id="cus_demo_guide",
            payment_method_status=PaymentMethodStatus.vaulted.value,
        )
    )

    # Book into the next upcoming Kids class so "next booking" shows.
    inst = (
        db.session.query(ClassInstance)
        .join(ClassType, ClassInstance.class_type_id == ClassType.id)
        .filter(
            ClassInstance.client_account_id == ca.id,
            ClassInstance.status == InstanceStatus.scheduled.value,
            ClassInstance.starts_at_utc > now_utc(),
            ClassType.key.like("kids%"),
        )
        .order_by(ClassInstance.starts_at_utc)
        .first()
    )
    # Enrol in the group of the next upcoming Kids class so the schedule
    # filters to their bookable sessions. No class booked yet — the demo is
    # a PAID member who now goes and schedules their classes.
    cohort = inst.cohort_label if inst else None

    # Active (paid) membership: first charge already happened, next billing
    # ~4 weeks out. This is a member ready to RSVP into the schedule.
    plan = default_plan(ca.id)
    if plan:
        db.session.add(
            Subscription(
                client_account_id=ca.id,
                user_id=guardian.id,
                attendee_id=child.id,
                plan_id=plan.id,
                cohort_label=cohort,
                status=SubscriptionStatus.active.value,
                mrr_cents=plan.price_cents,
                activated_at=now_utc() - timedelta(days=3),
                first_charge_at=now_utc() - timedelta(days=3),
                current_period_end=now_utc() + timedelta(weeks=4),
            )
        )

    db.session.commit()
    print("=== DEMO MEMBER LOGIN READY ===")
    print("URL:      https://health.box2fit.com/portal/login")
    print(f"Email:    {DEMO_EMAIL}")
    print(f"Password: {password}")
    print(f"Child: Demo Kid | Membership: ACTIVE (paid){' · ' + cohort if cohort else ''}")
    print("No class booked yet — log in and use 'Open the schedule' to book.")
    print("Remove later with:  .venv/bin/python -m scripts.make_demo_login --remove")
