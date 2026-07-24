"""Outbox → Meta CAPI + GA4 Measurement Protocol.

- event_id is shared with the browser pixel for dedup.
- User data is SHA-256 hashed after normalization per Meta spec.
- Exponential backoff: retry after 2^attempts minutes, capped attempts.
- Unconfigured destinations mark events dispatched with a note so the
  outbox never grows unbounded in dev.
"""
import hashlib
import json
import logging
import urllib.request
from datetime import timedelta

from flask import current_app

from ..extensions import db
from ..models import EventOutbox, utcnow

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
META_EVENT_MAP = {
    "Lead": "Lead",
    "Schedule": "Schedule",
    "AddPaymentInfo": "AddPaymentInfo",
    "SubscriptionActivated": "SubscriptionActivated",  # custom event
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_user_data(payload: dict) -> dict:
    """Normalized + hashed identifiers per Meta CAPI spec."""
    out = {}
    email = (payload.get("email") or "").strip().lower()
    phone = (payload.get("phone") or "").strip().lstrip("+")
    if email:
        out["em"] = [_sha256(email)]
    if phone:
        out["ph"] = [_sha256(phone)]
    return out


def _post_json(url: str, body: dict, timeout: int = 10) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def send_meta(event: EventOutbox) -> bool:
    pixel_id = current_app.config["META_PIXEL_ID"]
    token = current_app.config.get("META_CAPI_ACCESS_TOKEN") or ""
    if not pixel_id or not token:
        return False
    name = META_EVENT_MAP.get(event.event_name)
    if name is None:
        return False  # internal-only event, no pixel mirror
    payload = event.payload or {}
    data = {
        "event_name": name,
        "event_time": int(event.created_at.timestamp()),
        "event_id": event.event_id,  # dedup key shared with the pixel
        "action_source": "website",
        "user_data": hash_user_data(payload),
        "custom_data": {
            k: v
            for k, v in payload.items()
            if k in ("value", "currency", "segment", "utm_campaign", "utm_content")
            and v is not None
        },
    }
    _post_json(
        f"https://graph.facebook.com/v19.0/{pixel_id}/events?access_token={token}",
        {"data": [data]},
    )
    return True


def send_ga4(event: EventOutbox) -> bool:
    mid = current_app.config["GA4_MEASUREMENT_ID"]
    secret = current_app.config.get("GA4_API_SECRET") or ""
    if not mid or not secret:
        return False
    payload = event.payload or {}
    client_id = str(payload.get("lead_id") or payload.get("user_id") or event.id)
    _post_json(
        f"https://www.google-analytics.com/mp/collect?measurement_id={mid}"
        f"&api_secret={secret}",
        {
            "client_id": f"b2f.{client_id}",
            "events": [
                {
                    "name": event.event_name.lower(),
                    "params": {
                        "event_id": event.event_id,
                        "value": payload.get("value"),
                        "currency": payload.get("currency"),
                        "segment": payload.get("segment"),
                        "utm_campaign": payload.get("utm_campaign"),
                        "utm_content": payload.get("utm_content"),
                    },
                }
            ],
        },
    )
    return True


def drain(limit: int = 100) -> int:
    """Dispatch pending outbox events with exponential backoff."""
    now = utcnow()
    meta_on = bool(
        current_app.config["META_PIXEL_ID"]
        and current_app.config.get("META_CAPI_ACCESS_TOKEN")
    )
    ga_on = bool(
        current_app.config["GA4_MEASUREMENT_ID"]
        and current_app.config.get("GA4_API_SECRET")
    )
    pending = (
        db.session.query(EventOutbox)
        .filter(EventOutbox.dispatched_at.is_(None))
        .order_by(EventOutbox.id)
        .limit(limit)
        .all()
    )
    done = 0
    for event in pending:
        if event.last_attempt_at is not None:
            wait = timedelta(minutes=2 ** min(event.attempts, 9))
            if now < event.last_attempt_at + wait:
                continue
        if not meta_on and not ga_on:
            event.dispatched_at = now
            event.last_error = "skipped_not_configured"
            done += 1
            continue
        try:
            event.last_attempt_at = now
            sent_meta = send_meta(event)
            sent_ga = send_ga4(event)
            event.dispatched_at = now
            event.last_error = None if (sent_meta or sent_ga) else "no_destination"
            done += 1
        except Exception as exc:
            event.attempts += 1
            event.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if event.attempts >= MAX_ATTEMPTS:
                event.dispatched_at = now  # give up; error retained
            log.warning("outbox dispatch failed id=%s: %s", event.id, exc)
    return done
