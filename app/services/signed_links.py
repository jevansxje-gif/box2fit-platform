"""One-click signed URLs (itsdangerous) — cancel booking, cancel-before-charge,
unsubscribe, set-password invites. Per-action salts; no login required."""
from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SALT_CANCEL_BOOKING = "cancel-booking"
SALT_CANCEL_BEFORE_CHARGE = "cancel-before-charge"
SALT_UNSUBSCRIBE = "unsubscribe"
SALT_SET_PASSWORD = "set-password"
SALT_MAGIC_LOGIN = "magic-login"
SALT_WAITLIST_CONFIRM = "waitlist-confirm"
SALT_UPDATE_CARD = "update-card"
SALT_ACTIVATE = "activate-membership"
SALT_CONFIRM_ATTEND = "confirm-attendance"

MAX_AGE = 60 * 60 * 24 * 90  # 90 days default


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=salt)


def make_token(record_id: int, salt: str) -> str:
    return _serializer(salt).dumps(record_id)


def read_token(token: str, salt: str, max_age: int = MAX_AGE) -> int | None:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
