"""Environment-driven configuration. All policy values are config so the gym
can introduce cutoffs later without code changes (resolved policy: RSVP model,
everything defaults to 'off')."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

LOCAL_TZ = "America/Vancouver"  # all schedule math local, storage UTC


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    # Canonical public origin for signed cancel links and email links.
    # SITE_BASE_URL preferred; BASE_URL accepted per the deploy brief.
    SITE_BASE_URL = (
        os.environ.get("SITE_BASE_URL")
        or os.environ.get("BASE_URL")
        or "http://localhost:5001"
    )

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'platform_dev.db'}"
    )
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")
    CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "0") == "1"

    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
    SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "hello@box2fit.com")
    SENDGRID_FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "Box2Fit White Rock")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

    META_PIXEL_ID = os.environ.get("META_PIXEL_ID", "")
    GA4_MEASUREMENT_ID = os.environ.get("GA4_MEASUREMENT_ID", "")
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")

    JWT_SECRET = os.environ.get("JWT_SECRET", os.environ.get("SECRET_KEY", "dev-jwt"))
    JWT_EXPIRES_MINUTES = _int("JWT_EXPIRES_MINUTES", 43200)

    # --- Booking policy (RSVP model — resolved: all cutoffs off) ---
    POLICY_BOOKING_CUTOFF_MIN = _int("POLICY_BOOKING_CUTOFF_MIN", 0)
    POLICY_CANCEL_CUTOFF_MIN = _int("POLICY_CANCEL_CUTOFF_MIN", 0)
    POLICY_LATE_CANCEL_HOURS = _int("POLICY_LATE_CANCEL_HOURS", 12)
    POLICY_NOSHOW_AUTOMARK_MIN = _int("POLICY_NOSHOW_AUTOMARK_MIN", 30)
    POLICY_WAITLIST_CONFIRM_HOURS = _int("POLICY_WAITLIST_CONFIRM_HOURS", 2)
    # Members are enrolled in one group (cohort) and cannot book the other
    # group's days (client decision 2026-07-24). Config so it can flip later.
    POLICY_ALLOW_CROSS_GROUP = os.environ.get("POLICY_ALLOW_CROSS_GROUP", "0") == "1"

    SCHEDULE_HORIZON_WEEKS = _int("SCHEDULE_HORIZON_WEEKS", 4)

    # First charge lands this many hours after activation; the pre-charge
    # reminder (card-network trial rule) goes out at activation, which is by
    # construction this far ahead of the charge.
    PRE_CHARGE_LEAD_HOURS = _int("PRE_CHARGE_LEAD_HOURS", 48)
    STRIPE_STATEMENT_DESCRIPTOR = os.environ.get(
        "STRIPE_STATEMENT_DESCRIPTOR", "BOX2FIT WHITE ROCK"
    )

    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    UTM_COOKIE_NAME = "b2f_attr"
    UTM_COOKIE_MAX_AGE = 30 * 24 * 3600
    WTF_CSRF_TIME_LIMIT = None


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    WTF_CSRF_ENABLED = False
    CELERY_TASK_ALWAYS_EAGER = True
    RATELIMIT_ENABLED = False
