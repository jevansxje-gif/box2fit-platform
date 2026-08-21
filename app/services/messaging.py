"""Outbound email (SendGrid) / SMS (Twilio). Every send logs a messages row
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
        body_preview=html[:2000],
    )
    if not transactional and user is not None and not user.consent_email:
        msg.delivery_status = "suppressed_no_consent"
        msg.unsubscribe_honored = True
        db.session.add(msg)
        return msg

    smtp_host = current_app.config["SMTP_HOST"]
    api_key = current_app.config["SENDGRID_API_KEY"]
    if smtp_host:
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

    sid = current_app.config["TWILIO_ACCOUNT_SID"]
    if not sid:
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
