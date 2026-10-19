# Box2Fit Platform (health.box2fit.com)

Read this first. It is the working agreement for any Claude session in this repo. The long
history lives in the memory notes (`project_box2fit_platform.md`) and in
`W:\Documents\Box2Fit\2026-10-14 Box2Fit - handover for a new Claude session.md`.

## What this is
One Flask app for the Box2Fit White Rock boxing gym: marketing funnel + booking, guardian
and member portal, gym ops back office, JSON API, Stripe billing. Joey (the user) runs the
marketing and the platform for the owner, Frankie LaSasso, on 25% of pre-tax revenue.
Kids and youth were the first journey; adults run under one banner, Guided Boxing.

## Stack and layout
- Python 3.12, Flask, SQLAlchemy, Alembic (SQLite in production), Celery eager mode,
  Jinja templates, `app/static/css/platform.css` (pf-* components) + `site.css`.
- `app/blueprints/`: `funnel.py` (public site, landing pages, booking, challenge, She Hits,
  one-off pay links, Guided Start), `portal.py`, `ops_admin.py` (/ops), `api.py` (/api/v1,
  Stripe webhook router).
- `app/services/`: `billing.py` (subscriptions, webhooks, one-off charges), `challenge.py`
  (CHALLENGE + SHEHITS_INTRO offer configs, checkout, finalize, welcome, renewal reminders),
  `stripe_service.py`, `booking_flow.py` (free week rule, confirmations), `scheduling.py`,
  `class_admin.py`, `guided.py` (Guided Start questionnaire), `tax.py` (5% GST, half-up),
  `messaging.py` (Brevo API first; SMTP is blocked on the droplet).
- `content/copy/*.yaml`: landing-page copy (youth, shehits, guided-boxing, beast).
  Objection keys match the ads' `?v=` codes (g1..g7, s1..s2, f1..f5).
- `scripts/`: seed (class types, schedule, plans; reconciles ONCE per SCHEDULE_VERSION),
  `schedule_status` (read-only report), `set_start <key> <date>` (launch date per class),
  `move_class <key> HH:MM`, `check_card <name> [--fix]`, `backfill_payments`,
  `reconcile_cards`, `comms_audit <name>`, `ads_readout`, `purge_test_data`, `run_jobs`.
- Tests: `.venv\Scripts\python.exe -m pytest -q tests` (78 passing as of 2026-10-05).
  Run them before every push.
- Local preview: launch.json name `box2fit-platform`, http://localhost:5001 (restart the
  server after template edits; templates are cached when debug is off).

## Production
- Droplet "Boxing" 178.128.153.65, app at `/website/box2fit-platform`, venv `.venv`,
  systemd service **`website`**, nginx in front. Logs: `/var/log/nginx/box2fit.access.log`.
  Nightly DB backup 03:10 to `/website/backups`; `run_jobs` cron every 10 min.
- Secrets live only in the server's `.env`; never paste them into chat. After any `.env`
  change `systemctl restart website` is mandatory.
- Stripe: live account, API pinned **2019-09-09**. In scripts read objects with
  `json.loads(str(obj))` (this stripe-python has no `.get()`), and pin `api_version`.
  The webhook endpoint "box2fit-program" must subscribe to: setup_intent.succeeded,
  invoice_payment.paid (NOT invoice.paid), invoice.payment_failed,
  customer.subscription.deleted, charge.refunded, checkout.session.completed. Hosted
  Checkout calls use `stripe_version="2023-10-16"` per request.
- Meta pixel 1372615567700770. Events: PageView everywhere, Lead + Schedule on booking
  confirmation, Purchase on the challenge and She Hits thank-you pages.

## Deploy cadence
1. Commit with a clear message. Run the tests. Push to `main`
   (`https://github.com/jevansxje-gif/box2fit-platform.git`).
2. Give Joey ONE PuTTY line for the Boxing droplet, ending `&& echo "ON BOXING - DEPLOYED"
   && systemctl is-active website`, and state the expected output. Include
   `FLASK_APP=wsgi.py .venv/bin/flask db upgrade` when a migration is in the push and
   `.venv/bin/python -m scripts.seed` when class types, schedule or plans changed.
   Standard line:
   `cd /website/box2fit-platform && git pull -q && FLASK_APP=wsgi.py .venv/bin/flask db upgrade 2>&1 | tail -1 && systemctl restart website && echo "ON BOXING - DEPLOYED" && systemctl is-active website`
3. Verify on the live site with curl or the browser and report what was checked.
4. Never run Box2Fit commands on the PeritiaPlay droplet; the prompt must say "Boxing".

## Rules that came from the owner and from Joey
- Prices are pre-tax and shown "+ GST"; the agency share is 25% of pre-tax.
- Memberships: $189 + GST a month, on the same day each month (every 4 weeks until
  2026-10-19), 30-day written cancellation notice. Never write
  "cancel anytime" about a membership.
- The 5-Week Guided Boxing Challenge: $249 + GST, then $249 + GST every 5 weeks on its own
  plan; gloves, wraps and a one-to-one coach session included; auto-renewal stays.
- She Hits: two weeks for $94.50 + GST, then the membership; auto-renewal stays.
- Kids: Mon/Wed/Fri 4 pm only. Adults: Guided Boxing 6 AM (challenge), 5 PM, 6 PM;
  She Hits 9 AM. Classes that had not started opened Monday 26 October 2026.
- No em dashes in anything the gym or its customers read. Plain English.
- Ops instructions to Joey: one exact step at a time, window label first (PowerShell /
  PuTTY / Browser), expected output stated. Give full absolute Windows paths.
- Deliverables go to `W:\Documents\Box2Fit\` with dated plain-English names.
- Do not add work for the gym (content, releases, interviews) without being asked;
  media releases were settled by Joey directly with the gym.
