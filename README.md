# Box2Fit Platform v1

One Flask application: marketing funnel + member portal + gym operations
backend + JSON API (Phase 2 mobile app reuses it). Agency command center
(separate brief) reads the same database. Kids/youth classes launch first —
the guardian/child path is the primary journey.

## Status

- **Pass 1 — Kids-first funnel core: built.** Full data-model migration,
  youth landing + guardian/child booking flow with health questionnaire and
  guardian-signed waiver, Stripe SetupIntent card vault, UTM capture,
  reminders (T-24h/T-2h), signed one-click cancel, ops Today view with
  rosters + attendance, seed data, gating E2E green.
- **Pass 2 — Money lifecycle: built.** Activation (ops button + member
  self-confirm link from the post-class email) creates the 4-week $189
  subscription on the vaulted card with a 48h first-charge lead; pre-charge
  reminder with one-click cancel sent at activation (card-network trial
  rule); full webhook set (`setup_intent.succeeded`, `invoice.paid`,
  `invoice.payment_failed` + dunning, `customer.subscription.deleted`,
  `charge.refunded`); payments write `agency_share_cents` at the client's
  25% commission; past-due blocks new bookings (existing honored) with a
  signed self-serve card-update page; refunds reverse the agency share on
  the net. Gating E2E green.
- **Pass 3 — Remaining funnel + portal: built.** Five copy-config landing
  pages (/strong /reset /focus /shehits /beast); guardian portal with
  password + magic-link auth, set-password invites, dashboard (next class,
  quick-book, membership status, referral code), going/not-going RSVP
  schedule with group lock, waitlist auto-promotion with 2h confirm-or-
  release, my-classes streaks, membership management + cancel flow with
  reason capture (incl. the pause-would-have-saved-me option), waiver
  re-sign, notification preferences; public site (home, schedule, trainers,
  pricing, contact, privacy, terms + LocalBusiness schema); kiosk check-in
  with first-timer state; member booking API (JWT) for the Phase 2 app.
- **Pass 4 — Ops depth + tracking + comms: built.** Schedule builder
  (templates, closures, per-instance overrides, cancel-class with member
  notifications + nearest-equivalent offer + waitlist dissolve), trainer
  management with conflict detection and substitutions ("Now coached by…"
  notices, sub history logged), member directory + detail (notes timeline,
  flags: first-timer / 14-day at-risk / high no-show, read-only source
  attribution, resend invite, staff-initiated cancel), reporting (fill
  heatmap, attendance trend, churn by reason, trial conversion by class &
  trainer, no-show/late-cancel report, MRR) with CSV exports, Meta CAPI +
  GA4 outbox dispatch (event_id dedup, SHA-256 hashed identifiers,
  exponential backoff), call-tracking webhook + phone matching with
  attributed conversions, announcements (portal banner + consent-respecting
  email), review CRUD + aggregate badge. **Final full-platform E2E green.**
- v1 feature-complete. See `CHANGES.md` for decisions and open questions.

## Blueprints

| Blueprint | Mount | Pass 1 state |
|---|---|---|
| funnel | / | public site + all six landing pages + booking flow + activation/cancel links |
| portal | /portal | full guardian portal (auth, RSVP schedule, waitlist, membership, referrals) |
| ops | /ops | Today view, kiosk, schedule builder, trainers, members, reports, announcements, reviews |
| api | /api/v1 | JWT auth, me, classes, member bookings, Stripe + call-tracking webhooks |
| agency | /agency | stub (separate brief, same DB) |

Portal demo login (dev): `demo.member@example.com` / `demo12345` (child Emma,
Group A active membership).

## Local dev (Windows)

```powershell
cd C:\work\box2fit-platform
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
$env:FLASK_APP = "wsgi.py"
.venv\Scripts\flask db upgrade
.venv\Scripts\python -m scripts.seed
.venv\Scripts\python wsgi.py     # http://localhost:5001/youth
```

No `DATABASE_URL` → SQLite fallback (`platform_dev.db`); staging/prod set
MariaDB. No Stripe keys → dev-skip button on the card step. No SendGrid/
Twilio keys → messages log to the `messages` table (CASL trail), skipped on
the wire.

Ops login (dev seed): `frontdesk@box2fit.local` or `admin@box2fit.local`,
password `box2fit-dev` → http://localhost:5001/ops/today

Celery (Redis required, or `CELERY_TASK_ALWAYS_EAGER=1`):

```powershell
celery -A celery_worker worker -l info --pool=solo
celery -A celery_worker beat -l info
```

Beat schedule: reminders every 10 min · schedule generation nightly 03:15 ·
no-show automark every 15 min.

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

`tests/test_pass1_e2e.py::test_e2e_ad_click_to_attendance` is the Pass 1 gate:
ad-click URL with UTMs → child booking → card vaulted → reminder queued →
attendance marked.

## Architecture notes

- **Guardian/child**: `users` (adults; payment, consents, comms) vs
  `attendee_profiles` (self or child; bookings, waivers, attendance). Children
  carry minimum data (PIPEDA): first name, birth year, emergency contact,
  guardian-completed health questionnaire. All messages go to the guardian.
- **Multi-client**: `client_account_id` on every business table from
  migration zero. Commission rate (0.25) lives on `client_accounts`.
- **Schedule**: weekly `schedule_templates` (local time, America/Vancouver) →
  generated `class_instances` (UTC storage) N weeks ahead; closure dates
  suppress generation; per-instance overrides ready (capacity, trainer, room).
- **Policy is config**: RSVP model — booking open to class start, cancel
  anytime, no penalties. Late-cancel (<12h) and no-shows tracked silently for
  staff reporting only. All cutoffs are env vars defaulting to off.
- **Pricing per plan** (`plans` table), never hardcoded. Current Unlimited
  $189/mo is a flagged placeholder; youth rate unconfirmed.
- **API-first**: member-facing actions land in `api` (JWT) for the Phase 2
  app; web consumes the same services.
- Card data touches Stripe Elements only; UTMs mirror into Stripe metadata.
