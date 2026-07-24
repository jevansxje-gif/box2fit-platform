"""Conversion events → outbox. Celery drains to Meta CAPI + GA4 in Pass 4;
producers only enqueue, so tracking can never break the funnel."""
from ..extensions import db
from ..models import EventOutbox, Lead


def enqueue_event(
    event_name: str,
    client_account_id: int,
    lead: Lead | None = None,
    **extra,
) -> EventOutbox:
    payload = dict(extra)
    if lead is not None:
        payload.update(
            {
                "lead_id": lead.id,
                "email": lead.email,
                "phone": lead.phone,
                "segment": lead.segment,
                "utm_source": lead.utm_source,
                "utm_medium": lead.utm_medium,
                "utm_campaign": lead.utm_campaign,
                "utm_content": lead.utm_content,
                "utm_term": lead.utm_term,
                "landing_variant": lead.landing_variant,
            }
        )
    evt = EventOutbox(
        event_name=event_name, client_account_id=client_account_id, payload=payload
    )
    db.session.add(evt)
    return evt
