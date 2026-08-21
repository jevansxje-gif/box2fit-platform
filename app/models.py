"""Box2Fit Platform data model (Pass 1: full schema).

Structural rules from the spec:
- Multi-client scoping via client_account_id on every business table.
- Guardian/child: the account holder (User) is always an adult. Bookings,
  waivers, attendance and check-ins attach to AttendeeProfile; payment
  methods, subscriptions, consents and communications attach to the User.
- Children carry minimum data (PIPEDA): first name, birth year, emergency
  contact, guardian-completed health questionnaire. No child email/phone.
- Status/kind fields are strings validated by the enums below (not DB enums)
  so MariaDB migrations stay painless.
- Timestamps are stored naive-UTC; schedule math happens in America/Vancouver
  via services.tzutil.
"""
import enum
import uuid
from datetime import date, datetime, time, timezone

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(str, enum.Enum):
    member = "member"
    front_desk = "front_desk"
    trainer = "trainer"
    gym_admin = "gym_admin"
    agency_admin = "agency_admin"


class AttendeeKind(str, enum.Enum):
    self_ = "self"
    child = "child"


class LeadStatus(str, enum.Enum):
    new = "new"
    booked = "booked"
    attended = "attended"
    activated = "activated"
    cancelled = "cancelled"
    lost = "lost"


class BookingStatus(str, enum.Enum):
    booked = "booked"
    cancelled = "cancelled"
    attended = "attended"
    no_show = "no_show"


class BookingKind(str, enum.Enum):
    trial = "trial"
    member = "member"
    comp = "comp"
    walkin = "walkin"


class InstanceStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class SubscriptionStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    past_due = "past_due"
    paused = "paused"  # schema-ready; pause not offered in v1
    cancelled = "cancelled"


class PaymentMethodStatus(str, enum.Enum):
    none = "none"
    vaulted = "vaulted"
    failed = "failed"


# ---------------------------------------------------------------- scoping ---
class ClientAccount(db.Model):
    __tablename__ = "client_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    commission_rate: Mapped[float] = mapped_column(default=0.25)
    timezone: Mapped[str] = mapped_column(String(60), default="America/Vancouver")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------- accounts & people ---
class User(UserMixin, db.Model):
    """Adult account holder: guardian/member, or staff (role)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)  # E.164
    role: Mapped[str] = mapped_column(String(20), default=Role.member.value, index=True)

    # CASL consents (marketing) — transactional always allowed
    consent_email: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_captured_at: Mapped[datetime | None] = mapped_column(DateTime)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime)
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True)
    # Per-category notification prefs {"reminders": true, "schedule": true,
    # "news": false} — marketing consent stays separate (CASL).
    notify_prefs: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    attendees: Mapped[list["AttendeeProfile"]] = relationship(back_populates="guardian")

    __table_args__ = (
        UniqueConstraint("client_account_id", "email", name="uq_user_email"),
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(
            self.password_hash, password
        )

    def is_staff(self) -> bool:
        return self.role in (
            Role.front_desk.value,
            Role.trainer.value,
            Role.gym_admin.value,
            Role.agency_admin.value,
        )


class AttendeeProfile(db.Model):
    """Who actually attends class: the guardian themselves, or a child."""

    __tablename__ = "attendee_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default=AttendeeKind.child.value)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80))
    birth_year: Mapped[int | None] = mapped_column(Integer)  # minimum child data
    emergency_contact_name: Mapped[str | None] = mapped_column(String(120))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(20))
    # Guardian-completed health questionnaire {questions, answers, notes}
    health_json: Mapped[dict | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    guardian: Mapped[User] = relationship(back_populates="attendees")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="attendee")

    def age_in_year(self, year: int) -> int | None:
        # Birth-year-only data means age is approximate by calendar year —
        # exactly the granularity youth brackets need.
        return None if self.birth_year is None else year - self.birth_year

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name or ''}".strip()


class Trainer(db.Model):
    __tablename__ = "trainers"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # login link
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))  # assignment notices
    role_title: Mapped[str | None] = mapped_column(String(120))
    bio: Mapped[str | None] = mapped_column(Text)
    photo: Mapped[str | None] = mapped_column(String(255))
    certifications: Mapped[list | None] = mapped_column(JSON)  # ["NCCP Boxing", ...]
    class_type_keys: Mapped[list | None] = mapped_column(JSON)  # can-lead list
    pay_rate_cents: Mapped[int | None] = mapped_column(Integer)  # v1.1, schema-ready
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ------------------------------------------------------ classes & schedule ---
class ClassType(db.Model):
    __tablename__ = "class_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    key: Mapped[str] = mapped_column(String(40))  # youth_7_10, she_hits, ...
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    segment_tag: Mapped[str | None] = mapped_column(String(40))  # marketing segment
    age_min: Mapped[int | None] = mapped_column(Integer)  # youth bracket, inclusive
    age_max: Mapped[int | None] = mapped_column(Integer)
    duration_min: Mapped[int] = mapped_column(Integer, default=45)
    default_capacity: Mapped[int] = mapped_column(Integer, default=12)
    color: Mapped[str | None] = mapped_column(String(20))
    accepts_trials: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("client_account_id", "key", name="uq_class_type_key"),
    )

    @property
    def is_youth(self) -> bool:
        return self.age_min is not None or self.age_max is not None

    def age_bracket_label(self) -> str | None:
        if not self.is_youth:
            return None
        return f"Ages {self.age_min}–{self.age_max}"


class Plan(db.Model):
    """Membership pricing per class-type/plan — never hardcoded."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    class_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("class_types.id")
    )  # null = all-access
    name: Mapped[str] = mapped_column(String(120))
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    interval: Mapped[str] = mapped_column(String(10), default="month")
    stripe_price_id: Mapped[str | None] = mapped_column(String(64))
    # Placeholder pricing pending client confirmation (youth rate!)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduleTemplate(db.Model):
    """Weekly recurring slot; instances are generated N weeks ahead."""

    __tablename__ = "schedule_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    class_type_id: Mapped[int] = mapped_column(ForeignKey("class_types.id"))
    # Enrollment cohort ("Group A", "Group B", ...). Members join a group and
    # RSVP per session; groups are expandable without schema changes.
    cohort_label: Mapped[str | None] = mapped_column(String(40))
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon (local)
    start_time_local: Mapped[time] = mapped_column(Time)
    duration_min: Mapped[int | None] = mapped_column(Integer)  # default: class type
    capacity: Mapped[int | None] = mapped_column(Integer)  # default: class type
    trainer_id: Mapped[int | None] = mapped_column(ForeignKey("trainers.id"))
    accepts_trials: Mapped[bool | None] = mapped_column(Boolean)  # default: type
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    class_type: Mapped[ClassType] = relationship()
    trainer: Mapped[Trainer | None] = relationship()


class ClosureDate(db.Model):
    """Holiday/closure — suppresses instance generation for that local date."""

    __tablename__ = "closure_dates"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    closed_on: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(String(200))


class ClassInstance(db.Model):
    """A concrete class on the calendar; bookings attach here."""

    __tablename__ = "class_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_templates.id"), index=True
    )
    class_type_id: Mapped[int] = mapped_column(ForeignKey("class_types.id"), index=True)
    cohort_label: Mapped[str | None] = mapped_column(String(40), index=True)
    trainer_id: Mapped[int | None] = mapped_column(ForeignKey("trainers.id"))
    starts_at_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    local_time: Mapped[time] = mapped_column(Time)
    duration_min: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    room: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(
        String(15), default=InstanceStatus.scheduled.value, index=True
    )
    accepts_trials: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    class_type: Mapped[ClassType] = relationship()
    trainer: Mapped[Trainer | None] = relationship()
    bookings: Mapped[list["Booking"]] = relationship(back_populates="class_instance")

    __table_args__ = (
        UniqueConstraint("template_id", "local_date", name="uq_instance_occurrence"),
    )


# ------------------------------------------------------------------ funnel ---
class Lead(db.Model):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))  # the guardian/adult
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    segment: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(
        String(15), default=LeadStatus.new.value, index=True
    )
    utm_source: Mapped[str | None] = mapped_column(String(120))
    utm_medium: Mapped[str | None] = mapped_column(String(120))
    utm_campaign: Mapped[str | None] = mapped_column(String(255))
    utm_content: Mapped[str | None] = mapped_column(String(255))
    utm_term: Mapped[str | None] = mapped_column(String(255))
    landing_variant: Mapped[str | None] = mapped_column(String(80))
    referral_code: Mapped[str | None] = mapped_column(String(20), index=True)
    first_touch_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- bookings ---
class Booking(db.Model):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    attendee_id: Mapped[int] = mapped_column(
        ForeignKey("attendee_profiles.id"), index=True
    )
    class_instance_id: Mapped[int] = mapped_column(
        ForeignKey("class_instances.id"), index=True
    )
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    kind: Mapped[str] = mapped_column(String(10), default=BookingKind.trial.value)
    status: Mapped[str] = mapped_column(
        String(12), default=BookingStatus.booked.value, index=True
    )
    booked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    late_cancel: Mapped[bool] = mapped_column(Boolean, default=False)  # staff-only
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Pre-class "I'm coming" from the reminder email — NOT attendance;
    # attendance is marked at the door (kiosk/staff).
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    attendance_marked_by: Mapped[str | None] = mapped_column(String(120))
    ipad_walkin: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminder_2h_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    attendee: Mapped[AttendeeProfile] = relationship(back_populates="bookings")
    class_instance: Mapped[ClassInstance] = relationship(back_populates="bookings")

    __table_args__ = (
        UniqueConstraint("attendee_id", "class_instance_id", name="uq_booking"),
    )


class WaitlistEntry(db.Model):
    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    class_instance_id: Mapped[int] = mapped_column(
        ForeignKey("class_instances.id"), index=True
    )
    attendee_id: Mapped[int] = mapped_column(ForeignKey("attendee_profiles.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="waiting")
    offered_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ----------------------------------------------------------------- waivers ---
class WaiverDocument(db.Model):
    """Versioned waiver text; a version bump triggers re-sign in the portal."""

    __tablename__ = "waiver_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10), default="minor")  # adult | minor
    version: Mapped[int] = mapped_column(Integer, default=1)
    body_md: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("client_account_id", "kind", "version", name="uq_waiver_ver"),
    )


class WaiverSignature(db.Model):
    """Guardian signs on the attendee's behalf (always, for minors)."""

    __tablename__ = "waiver_signatures"

    id: Mapped[int] = mapped_column(primary_key=True)
    attendee_id: Mapped[int] = mapped_column(
        ForeignKey("attendee_profiles.id"), index=True
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("waiver_documents.id"))
    signed_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    signature_name: Mapped[str] = mapped_column(String(120))  # typed legal name
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- payments ---
class StripeCustomer(db.Model):
    __tablename__ = "stripe_customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_method_status: Mapped[str] = mapped_column(
        String(10), default=PaymentMethodStatus.none.value
    )
    stripe_setup_intent_id: Mapped[str | None] = mapped_column(String(64))
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Subscription(db.Model):
    """One subscription per enrolled attendee, billed to the guardian."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # payer
    attendee_id: Mapped[int] = mapped_column(
        ForeignKey("attendee_profiles.id"), index=True
    )
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    # Which group the member is enrolled in (3 sessions/week within their
    # group; they RSVP per session). Nullable for non-cohort memberships.
    cohort_label: Mapped[str | None] = mapped_column(String(40))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(
        String(12), default=SubscriptionStatus.pending.value, index=True
    )
    mrr_cents: Mapped[int] = mapped_column(Integer, default=0)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime)
    first_charge_at: Mapped[datetime | None] = mapped_column(DateTime)  # trial end
    pre_charge_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 30-day notice (client's membership terms): when the member asked, and
    # when the cancellation takes effect. Dues continue until effective.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_effective_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(80))
    cancel_reason_note: Mapped[str | None] = mapped_column(Text)
    # Pause is NOT offered in v1 — schema stays ready (client may enable later)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    resumes_at: Mapped[datetime | None] = mapped_column(DateTime)


class Payment(db.Model):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    stripe_charge_id: Mapped[str | None] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)  # total collected, tax incl.
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)  # GST portion
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    status: Mapped[str] = mapped_column(String(15), default="paid")
    agency_share_cents: Mapped[int] = mapped_column(Integer, default=0)  # 25% of pre-tax
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------- comms, tracking, misc ---
class EventOutbox(db.Model):
    __tablename__ = "events_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    event_name: Mapped[str] = mapped_column(String(80), index=True)
    event_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), unique=True
    )
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Message(db.Model):
    """Outbound email/SMS log — the CASL audit trail. All communication goes
    to the guardian; attendee_id records who it was about."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    attendee_id: Mapped[int | None] = mapped_column(ForeignKey("attendee_profiles.id"))
    channel: Mapped[str] = mapped_column(String(10))
    template: Mapped[str] = mapped_column(String(120))
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(255))
    body_preview: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivery_status: Mapped[str] = mapped_column(String(40), default="queued")
    unsubscribe_honored: Mapped[bool] = mapped_column(Boolean, default=False)


class Call(db.Model):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    tracking_number: Mapped[str] = mapped_column(String(20))
    caller_number: Mapped[str] = mapped_column(String(20), index=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    recording_url: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    matched_lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    outcome: Mapped[str] = mapped_column(String(20), default="unknown")


class MemberNote(db.Model):
    """Staff-visible-only member memory: injuries, preferences, conversations."""

    __tablename__ = "member_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attendee_id: Mapped[int | None] = mapped_column(ForeignKey("attendee_profiles.id"))
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Announcement(db.Model):
    """Gym-wide or per-class-type broadcast. Shows as a portal banner;
    optional email respects the 'news' preference + CASL consent."""

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    class_type_id: Mapped[int | None] = mapped_column(ForeignKey("class_types.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Review(db.Model):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_account_id: Mapped[int] = mapped_column(
        ForeignKey("client_accounts.id"), index=True
    )
    reviewer_name: Mapped[str] = mapped_column(String(80))
    rating: Mapped[int] = mapped_column(Integer, default=5)
    quote_text: Mapped[str] = mapped_column(Text)  # verbatim, never paraphrased
    source: Mapped[str] = mapped_column(String(40), default="google")
    segment_tags: Mapped[str] = mapped_column(String(255), default="")
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def tags(self) -> set[str]:
        return {t.strip() for t in self.segment_tags.split(",") if t.strip()}


class SiteSetting(db.Model):
    __tablename__ = "site_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))

    @staticmethod
    def get(key: str, default: str = "") -> str:
        row = db.session.get(SiteSetting, key)
        return row.value if row else default

    @staticmethod
    def set(key: str, value: str) -> None:
        row = db.session.get(SiteSetting, key)
        if row:
            row.value = value
        else:
            db.session.add(SiteSetting(key=key, value=value))
