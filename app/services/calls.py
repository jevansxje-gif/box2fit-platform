"""Call tracking: CallRail-compatible webhook ingest + phone matching.
Matched calls whose lead later activates fire an attributed conversion."""
import logging

import phonenumbers

from ..extensions import db
from ..models import Call, Lead, LeadStatus
from .tracking import enqueue_event

log = logging.getLogger(__name__)


def normalize_phone(raw: str) -> str | None:
    try:
        parsed = phonenumbers.parse(raw or "", "CA")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def ingest(client_account_id: int, payload: dict) -> Call:
    """Accepts CallRail-style keys with fallbacks."""
    caller = normalize_phone(
        payload.get("customer_phone_number") or payload.get("caller_number") or ""
    ) or (payload.get("customer_phone_number") or payload.get("caller_number") or "")
    from datetime import datetime

    started_raw = payload.get("start_time") or payload.get("started_at")
    try:
        started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
        started = started.replace(tzinfo=None)
    except (ValueError, TypeError):
        from ..models import utcnow

        started = utcnow()
    call = Call(
        client_account_id=client_account_id,
        tracking_number=payload.get("tracking_phone_number")
        or payload.get("tracking_number")
        or "",
        caller_number=caller,
        duration_sec=int(payload.get("duration") or 0),
        recording_url=payload.get("recording") or payload.get("recording_url"),
        started_at=started,
    )
    db.session.add(call)
    db.session.flush()
    match(call)
    return call


def match(call: Call) -> Lead | None:
    """Link a call to a lead by normalized phone; attribute activation."""
    if call.matched_lead_id:
        return db.session.get(Lead, call.matched_lead_id)
    lead = (
        db.session.query(Lead)
        .filter_by(
            client_account_id=call.client_account_id, phone=call.caller_number
        )
        .order_by(Lead.id.desc())
        .first()
    )
    if lead is None:
        return None
    call.matched_lead_id = lead.id
    call.outcome = "matched"
    if lead.status == LeadStatus.activated.value:
        enqueue_event(
            "CallAttributedConversion", call.client_account_id, lead, call_id=call.id
        )
    return lead


def match_unmatched() -> int:
    """Beat job: retry matching for calls that arrived before their lead."""
    matched = 0
    for call in (
        db.session.query(Call).filter(Call.matched_lead_id.is_(None)).all()
    ):
        if match(call) is not None:
            matched += 1
    return matched
