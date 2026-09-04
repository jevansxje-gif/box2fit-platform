"""Outbound email (Brevo API → SMTP → SendGrid) / SMS (Brevo, template-
allowlisted via SMS_TEMPLATES; Twilio legacy). Every send logs a messages row
(CASL audit trail). All communication goes to the guardian — never to a child
(PIPEDA). Transactional sends ignore marketing consent; marketing sends check
it and record suppression."""
import logging

from flask import current_app

from ..extensions import db
from ..models import Message, User

log = logging.getLogger(__name__)


def send_email(
    user: User | None,
    to_email: str,
    subject: str,
    html: str,
    template: str,
    client_account_id: int,
    attendee_id: int | None = None,
    transactional: bool = True,
) -> Message:
    msg = Message(
        client_account_id=client_account_id,
        user_id=user.id if user else None,
        attendee_id=attendee_id,
        channel="email",
        template=template,
        recipient=to_email,
        subject=subject,
        body_preview=html[:4000],
    )
    if not transactional and user is not None and not user.consent_email:
        msg.delivery_status = "suppressed_no_consent"
        msg.unsubscribe_honored = True
        db.session.add(msg)
        return msg

    brevo_key = current_app.config["BREVO_API_KEY"]
    smtp_host = current_app.config["SMTP_HOST"]
    api_key = current_app.config["SENDGRID_API_KEY"]
    if brevo_key:
        try:
            _send_via_brevo_api(to_email, subject, html)
            msg.delivery_status = "sent"
        except Exception as exc:
            log.exception("brevo api send failed")
            msg.delivery_status = f"error:{type(exc).__name__}"
    elif smtp_host:
        try:
            _send_via_smtp(to_email, subject, html)
            msg.delivery_status = "sent"
        except Exception as exc:  # provider failure must never break the flow
            log.exception("smtp send failed")
            msg.delivery_status = f"error:{type(exc).__name__}"
    elif api_key:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            mail = Mail(
                from_email=(
                    current_app.config["MAIL_FROM_EMAIL"],
                    current_app.config["MAIL_FROM_NAME"],
                ),
                to_emails=to_email,
                subject=subject,
                html_content=html,
            )
            SendGridAPIClient(api_key).send(mail)
            msg.delivery_status = "sent"
        except Exception as exc:
            log.exception("sendgrid send failed")
            msg.delivery_status = f"error:{type(exc).__name__}"
    else:
        msg.delivery_status = "skipped_not_configured"
        log.info("email (dev, not sent) to=%s subject=%s", to_email, subject)
    db.session.add(msg)
    return msg


def _send_via_brevo_api(to_email: str, subject: str, html: str) -> None:
    """Brevo transactional API over HTTPS:443 — works where SMTP egress is
    blocked (DigitalOcean default). Raises on non-2xx."""
    import requests

    payload = {
        "sender": {
            "name": current_app.config["MAIL_FROM_NAME"],
            "email": current_app.config["MAIL_FROM_EMAIL"],
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
    }
    # Replies (cancellations, questions) go to the gym's real inbox —
    # the sending domain has no mailbox.
    reply_to = current_app.config.get("MAIL_REPLY_TO")
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": current_app.config["BREVO_API_KEY"],
            "content-type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()


def _send_via_smtp(to_email: str, subject: str, html: str) -> None:
    """Provider-agnostic SMTP (Brevo, or any relay) with STARTTLS."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr

    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = formataddr(
        (current_app.config["MAIL_FROM_NAME"], current_app.config["MAIL_FROM_EMAIL"])
    )
    m["To"] = to_email
    m.set_content("This message requires an HTML-capable email client.")
    m.add_alternative(html, subtype="html")
    with smtplib.SMTP(
        current_app.config["SMTP_HOST"], current_app.config["SMTP_PORT"], timeout=20
    ) as s:
        s.starttls()
        s.login(current_app.config["SMTP_USER"], current_app.config["SMTP_PASS"])
        s.send_message(m)


def _send_sms_via_brevo(to_number: str, body: str) -> None:
    """Brevo transactional SMS over HTTPS (prepaid credits on the account).
    Canadian/US carriers don't support alphanumeric senders — Brevo swaps
    the sender for a shared local number; that's expected. Raises on
    non-2xx (including 'no credits')."""
    import requests

    resp = requests.post(
        "https://api.brevo.com/v3/transactionalSMS/sms",
        headers={
            "api-key": current_app.config["BREVO_API_KEY"],
            "content-type": "application/json",
        },
        json={
            "type": "transactional",
            "sender": current_app.config["SMS_SENDER"],
            # Brevo wants country-code digits without the leading +
            "recipient": to_number.lstrip("+"),
            "content": body,
        },
        timeout=20,
    )
    resp.raise_for_status()


def send_sms(
    user: User | None,
    to_number: str,
    body: str,
    template: str,
    client_account_id: int,
    attendee_id: int | None = None,
    transactional: bool = True,
) -> Message:
    msg = Message(
        client_account_id=client_account_id,
        user_id=user.id if user else None,
        attendee_id=attendee_id,
        channel="sms",
        template=template,
        recipient=to_number,
        body_preview=body[:2000],
    )
    if not transactional and user is not None and not user.consent_sms:
        msg.delivery_status = "suppressed_no_consent"
        msg.unsubscribe_honored = True
        db.session.add(msg)
        return msg

    allowed = current_app.config["SMS_TEMPLATES"]
    brevo_key = current_app.config["BREVO_API_KEY"]
    sid = current_app.config["TWILIO_ACCOUNT_SID"]
    if brevo_key and ("*" in allowed or template in allowed):
        try:
            _send_sms_via_brevo(to_number, body)
            msg.delivery_status = "sent"
        except Exception as exc:
            log.exception("brevo sms send failed")
            msg.delivery_status = f"error:{type(exc).__name__}"
    elif not sid:
        msg.delivery_status = "skipped_not_configured"
        log.info("sms (dev, not sent) to=%s body=%s", to_number, body[:80])
    else:
        try:
            from twilio.rest import Client

            client = Client(sid, current_app.config["TWILIO_AUTH_TOKEN"])
            client.messages.create(
                to=to_number,
                from_=current_app.config["TWILIO_FROM_NUMBER"],
                body=body,
            )
            msg.delivery_status = "sent"
        except Exception as exc:
            log.exception("twilio send failed")
            msg.delivery_status = f"error:{type(exc).__name__}"
    db.session.add(msg)
    return msg
