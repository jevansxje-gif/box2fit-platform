"""Public funnel forms. Child path is the PRIMARY journey: the guardian books,
the child attends. Server-side validation on every endpoint; honeypot on
anything a bot could post."""
from datetime import date

import phonenumbers
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    HiddenField,
    IntegerField,
    PasswordField,
    RadioField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)

# Guardian-completed health questionnaire for the attendee. Youth-appropriate;
# answers stored verbatim on the attendee profile.
HEALTH_QUESTIONS = [
    ("asthma", "Does the attendee have asthma or any breathing condition?"),
    ("heart", "Has a doctor ever identified a heart condition or restricted their physical activity?"),
    ("bones", "Any bone, joint or muscle injury that could be aggravated by exercise?"),
    ("dizzy", "Do they experience dizziness, fainting or loss of balance during activity?"),
]


class HoneypotMixin:
    website = StringField("Website")  # hidden from humans

    def validate_website(self, field):
        if field.data:
            raise ValidationError("Invalid submission.")


class SlotForm(FlaskForm, HoneypotMixin):
    instance_id = IntegerField("Class", validators=[DataRequired()])


class AttendeeForm(FlaskForm, HoneypotMixin):
    # Who's attending — child first, always
    attendee_kind = RadioField(
        "Who is this class for?",
        choices=[("child", "My child"), ("self", "Myself")],
        default="child",
        validators=[DataRequired()],
    )

    # Child (attendee) — minimum data per PIPEDA
    child_first_name = StringField("Child's first name", validators=[Optional(), Length(1, 80)])
    child_birth_year = IntegerField("Child's birth year", validators=[Optional()])
    emergency_contact_name = StringField("Emergency contact name", validators=[Optional(), Length(max=120)])
    emergency_contact_phone = StringField("Emergency contact phone", validators=[Optional(), Length(max=25)])

    # Guardian (the account holder — always an adult)
    guardian_name = StringField("Your full name", validators=[DataRequired(), Length(2, 120)])
    email = StringField("Your email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Your mobile number", validators=[DataRequired(), Length(7, 25)])

    # Health questionnaire (guardian-completed)
    health_asthma = BooleanField()
    health_heart = BooleanField()
    health_bones = BooleanField()
    health_dizzy = BooleanField()
    health_notes = TextAreaField(
        "Anything else the coach should know? (allergies, conditions, notes)",
        validators=[Optional(), Length(max=2000)],
    )

    # CASL — unchecked by default, never pre-ticked
    consent_email = BooleanField("Email me schedules, tips and offers")
    consent_sms = BooleanField("Text me reminders and offers")

    # Waiver — guardian signs on the child's behalf
    waiver_agree = BooleanField(
        "I have read and agree to the liability waiver", validators=[DataRequired()]
    )
    signature = StringField(
        "Type your full legal name as your signature",
        validators=[DataRequired(), Length(2, 120)],
    )

    normalized_phone = HiddenField()
    normalized_ec_phone = HiddenField()

    def validate_phone(self, field):
        self.normalized_phone.data = _normalize_phone(field.data)

    def validate_emergency_contact_phone(self, field):
        if field.data:
            self.normalized_ec_phone.data = _normalize_phone(field.data)

    def validate_child_first_name(self, field):
        if self.attendee_kind.data == "child" and not (field.data or "").strip():
            raise ValidationError("Please enter your child's first name.")

    def validate_child_birth_year(self, field):
        if self.attendee_kind.data != "child":
            return
        year = field.data
        current = date.today().year
        if year is None:
            raise ValidationError("Please enter your child's birth year.")
        if not (current - 18 <= year <= current - 4):
            raise ValidationError("Youth classes are for ages 4–18. Check the birth year.")

    def validate_emergency_contact_name(self, field):
        if self.attendee_kind.data == "child" and not (field.data or "").strip():
            raise ValidationError("An emergency contact is required for children.")

    def health_answers(self) -> dict:
        return {
            "questions": dict(HEALTH_QUESTIONS),
            "answers": {
                key: bool(getattr(self, f"health_{key}").data)
                for key, _ in HEALTH_QUESTIONS
            },
            "notes": (self.health_notes.data or "").strip(),
        }


class OpsLoginForm(FlaskForm):
    # No Email() validator here — strict validation belongs at account
    # creation; login just matches what's stored (incl. dev .local accounts).
    email = StringField("Email", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired()])


def _normalize_phone(raw: str) -> str:
    try:
        parsed = phonenumbers.parse(raw, "CA")
    except phonenumbers.NumberParseException:
        raise ValidationError("Enter a valid Canadian phone number.")
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError("Enter a valid Canadian phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
