"""Render every transactional email template with sample data into one
gallery HTML for review. Dev utility: python -m scripts._render_email_previews <out>
"""
import sys
from types import SimpleNamespace as NS

from flask import render_template

from app import create_app

guardian = NS(name="Sam Parent", email="sam.parent@example.com", phone="+1 604 555 0123")
attendee = NS(first_name="Maya", last_name="Parent", birth_year=2018)
trainer = NS(name="Frankie Torres")

EMAILS = [
    ("NEW · Admin: new sign-up alert", "emails/admin_new_signup.html", dict(
        attendee=attendee, guardian=guardian, is_child=True,
        class_name="Youth Boxing", cohort="Group A",
        when="Saturday Aug 23 at 4:00 PM",
        ops_url="https://health.box2fit.com/ops/today")),
    ("NEW · Customer: booking confirmation (with calendar buttons)", "emails/booking_confirmation.html", dict(
        guardian=guardian, attendee=attendee, is_child=True,
        class_name="Youth Boxing", when="Saturday Aug 23 at 4:00 PM",
        address="1522 Finlay St, Unit 216, White Rock, BC",
        cancel_url="#", gcal_url="#", ics_url="#")),
    ("NEW · Coach: assignment notice", "emails/coach_assignment.html", dict(
        trainer=trainer,
        lines=["Youth Boxing: every Monday at 4:00 PM",
               "Youth Boxing: every Wednesday at 4:00 PM",
               "She Hits on Thursday Aug 28 at 6:00 PM (this day only)"])),
    ("Reminder · 24 hours before class", "emails/reminder.html", dict(
        guardian=guardian, attendee=attendee, is_child=True, is_2h=False,
        class_name="Youth Boxing", when="Saturday Aug 23 at 4:00 PM",
        address="1522 Finlay St, Unit 216, White Rock, BC", cancel_url="#")),
    ("Post-class: 'Loved it?' with activation button", "emails/post_class.html", dict(
        guardian=guardian, attendee=attendee, is_child=True, activate_url="#")),
    ("Pre-charge reminder (48h before first charge)", "emails/pre_charge_reminder.html", dict(
        guardian=guardian, attendee=attendee, is_child=True, price="$189",
        lead_hours=48, cohort="Group A", cancel_url="#")),
    ("Membership welcome + portal invite", "emails/membership_welcome.html", dict(
        guardian=guardian, attendee=attendee, is_child=True, cohort="Group A",
        invite_url="#")),
    ("Dunning: card update needed", "emails/dunning.html", dict(
        guardian=guardian, attendee=attendee, update_url="#")),
    ("Payment recovered", "emails/payment_recovered.html", dict(guardian=guardian)),
    ("Waitlist: spot opened (confirm-or-release)", "emails/waitlist_promoted.html", dict(
        guardian=guardian, attendee=attendee, is_child=True,
        class_name="Youth Boxing", when="Monday Aug 25 at 4:00 PM", hours=2,
        confirm_url="#")),
    ("Schedule change: class moved", "emails/schedule_change.html", dict(
        guardian=guardian, attendee=attendee, class_name="Youth Boxing",
        old_when="Monday Aug 25 at 4:00 PM", new_when="Monday Aug 25 at 5:00 PM")),
    ("Class cancelled", "emails/class_cancelled.html", dict(
        guardian=guardian, attendee=attendee, class_name="Youth Boxing",
        when="Monday Aug 25 at 4:00 PM", alt_when="Wednesday Aug 27 at 4:00 PM")),
    ("Coach substitution notice", "emails/sub_notice.html", dict(
        guardian=guardian, attendee=attendee, class_name="Youth Boxing",
        when="Monday Aug 25 at 4:00 PM", coach="Frankie Torres")),
    ("Announcement broadcast", "emails/announcement.html", dict(
        user=guardian, a=NS(title="Holiday schedule",
                            body="We're closed Monday Sept 1 for Labour Day. All other classes run as usual."))),
    ("Magic sign-in link", "emails/magic_link.html", dict(user=guardian, url="#")),
]

app = create_app()
cards = []
with app.test_request_context():
    for title, tpl, ctx in EMAILS:
        html = render_template(tpl, **ctx)
        cards.append(
            f'<section style="margin:0 0 36px"><h2 style="font-family:Arial;font-size:15px;'
            f'letter-spacing:.06em;text-transform:uppercase;color:#555;margin:0 0 10px">{title}</h2>'
            f'<div style="border:1px solid #ddd;border-radius:10px;overflow:hidden;'
            f'box-shadow:0 2px 10px rgba(0,0,0,.06);background:#f6f6f6;padding:18px 0">{html}</div></section>'
        )

out = sys.argv[1] if len(sys.argv) > 1 else "email_previews.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(
        '<html><head><meta charset="utf-8"><title>Box2Fit email previews</title></head>'
        '<body style="background:#eee;margin:0;padding:30px 16px">'
        '<div style="max-width:640px;margin:0 auto">'
        '<h1 style="font-family:Arial;font-size:22px">Box2Fit — transactional email set</h1>'
        '<p style="font-family:Arial;color:#666;font-size:13px">Rendered from the live templates with sample data. Buttons are inert in this preview.</p>'
        + "".join(cards)
        + "</div></body></html>"
    )
print(f"wrote {out} with {len(cards)} emails")
