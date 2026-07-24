# Pass 4 — CHANGES (2026-07-24) — v1 feature-complete

## Decisions made

1. **Outbox dispatch**: unconfigured destinations mark events dispatched with
   `skipped_not_configured` so the table never grows unbounded in dev. Live
   dispatch: Meta CAPI (hashed em/ph per spec, shared event_id for pixel
   dedup) + GA4 MP mirror; exponential backoff `2^attempts` minutes, gives up
   (with error retained) after 8 attempts. Beat drains every 2 minutes.
2. **Call matching**: webhook accepts CallRail-style payloads; matches by
   E.164-normalized caller → most recent lead; calls arriving before their
   lead retry every 15 min; matched calls whose lead is activated enqueue a
   `CallAttributedConversion` event.
3. **Cancel class = one action**: notify every booked guardian (email + SMS)
   with the nearest same-type class offered, release bookings, dissolve the
   waitlist. Substitutions notify booked members and append to the
   instance's sub-history notes.
4. **Trainer conflicts**: template creation rejects overlapping assignments
   for the same trainer (time-window overlap on the same weekday).
5. **Flags**: first-timer = no attendance yet; at-risk = active/pending
   member with no visit in 14 days; high no-show = 3+ no-shows in 60 days
   (constant, easy to make config). Reports and no-show/late-cancel lists
   are staff-only, per the silent-tracking policy.
6. **Announcements**: portal banner immediately; the optional email respects
   the member's "news" preference AND CASL marketing consent (suppressions
   logged). Marketing campaigns proper stay in the agency command center.
7. **Bug fixed en route**: Flask-Login caches the resolved user on `g`; a
   long-lived app context (tests/scripts) leaked that cache across requests.
   A before_request reset (no-op in production) fixes it.

## Open questions / staging punch-list

- Stripe test keys → run the real card element + `stripe listen` end-to-end.
- Meta pixel id + CAPI token, GA4 id + secret → flip on live dispatch.
- Call-tracking provider account (CallRail or Twilio-based) → point its
  webhook at /api/v1/webhooks/calls.
- Waiver + privacy/terms legal text (placeholders in place).
- Real trainer roster, photos, bios; real timetable beyond Groups A/B.
- Per-group capacity cap; minimum age for adult classes (carried).
- MariaDB + Redis on staging per the hosting spec; change dev seed passwords.

---

# Pass 3 — CHANGES (2026-07-24)

## Decisions made

1. **Segment → class mapping** (needed a call; flag if wrong):
   /strong → Active Adults classes · /focus (professionals) → the midday
   Reset classes (its segment_tag moved to `focus`) · /shehits → She Hits ·
   /beast → Beast Camp · **/reset (parents) is a second front door for the
   youth funnel** — its CTA books youth classes, since parents booking kids
   IS the youth journey.
2. **Group lock enforced in portal + API**: a Group A membership can neither
   see nor book Group B days (config `POLICY_ALLOW_CROSS_GROUP`). Classes
   without a cohort (adult drop-ins) stay visible to everyone.
3. **Waitlist**: joining happens automatically when booking a full class;
   promotion auto-books + notifies with a 2h confirm-or-release window
   (config), a beat job releases expired offers and promotes the next entry.
4. **Magic-link login** (30-min expiry) works for every member — important
   because trial bookers have accounts but no password. Set-password invite
   goes out in the membership-welcome email.
5. **Referral codes** are FIRSTNAME-id, shown with copy/email/SMS share
   buttons; `?ref=CODE` is captured into first-touch attribution and onto
   the lead. Reward mechanics stay in the agency command-center scope.
6. **Streaks**: consecutive calendar weeks with ≥1 attended session +
   monthly visit count. No leaderboards (per spec).
7. **Kiosk** is a staff-session page (locked tablet at the door): name
   search over today's roster, giant green check-in, red first-timer
   variant that cues staff to greet them. QR scanning slots in later —
   USB scanners typing into the search box already work.
8. Privacy/terms pages are structured placeholders pending legal review.

## Open questions

- Should adult classes have a minimum age? Right now a child could be booked
  into e.g. Beast Camp because only youth classes carry age brackets.
- Reward terms for referrals (give-a-month/get-a-month?) — command-center
  brief territory, but the portal copy will need the promise text.
- Trainer photos/bios are placeholders on /trainers.

---

# Pass 2 — CHANGES (2026-07-24)

## Decisions made

1. **48h first-charge window**: the spec wants a pre-charge reminder 48h
   before the first charge. Implemented by creating the Stripe subscription
   with `trial_end = activation + 48h` (config `PRE_CHARGE_LEAD_HOURS`), and
   sending the reminder at activation — which is by construction exactly the
   lead time ahead of the charge. Deterministic, no scheduler race.
2. **Two activation paths**: front-desk button on the Today roster (attended
   trials with a vaulted card), and a member self-confirm signed link in the
   automatic post-class "loved it?" email. Both call the same service.
3. **4-week billing on Stripe**: `interval=week, interval_count=4`; the
   Stripe Price is created lazily from the plan and cached on it.
4. **Agency share**: written per payment as `round(amount ×
   client_accounts.commission_rate)` (25% → $47.25 per $189 cycle); refunds
   recompute the share on the net amount. Stripe invoice id is the
   idempotency key (webhook redelivery-safe).
5. **Past-due**: blocks NEW bookings only (existing honored, per policy);
   the block message points at the signed card-update link from the dunning
   email; recovery is webhook-driven — booking re-enables the moment
   `invoice.paid` lands. Dunning retries themselves are left to Stripe smart
   retries (per the Master Plan).
6. **Cancel-membership link** is one link for both cases: before first
   charge (reason `cancelled_before_charge`, no charge ever happens) and any
   time after (no contracts). Reason capture feeds churn analytics.
7. **Webhook receiver** lives at `POST /api/v1/webhooks/stripe`;
   signature-verified with `STRIPE_WEBHOOK_SECRET` when set, JSON-trusted in
   dev/test (documented).

## Open questions

- Stripe test-mode keys still needed to exercise the real card element and
  webhooks end-to-end against Stripe's CLI before staging.
- Statement descriptor "BOX2FIT WHITE ROCK" is config — confirm it matches
  the client's Stripe account setting (set on the account, not per-charge).
- Per-group capacity cap (carried from Pass 1 — currently 12).

---

# Pass 1 — CHANGES

## Post-review updates (2026-07-24, client answers)

1. **$189/month confirmed** — plan seeded as `Membership`, `is_placeholder=False`.
2. **Authoritative docs received** (Platform v1 Brief, Master Implementation
   Plan, Design Brief pasted in chat) — reconciled; no Pass 1 contradictions.
   Pass 2 gains specifics: pre-charge reminder is card-network mandatory,
   statement descriptor "BOX2FIT WHITE ROCK", Stripe = commission source of
   truth.
3. **Disclosure restored to the verbatim Master Plan §6 wording** with the
   $189 rate and the class date, and asserted verbatim in the E2E test.
4. **Real starting schedule seeded**: Group A Mon/Wed/Fri 4 PM, Group B
   Tue/Thu/Sat 4 PM. Groups are a first-class `cohort_label` on schedule
   templates, class instances, and subscriptions — adding Group C (or
   age-splitting groups) is a seed/ops change, no schema change. Membership
   structure: 3 sessions/week in your group, 4-week cycle; members RSVP per
   session (the RSVP data model from Pass 1; the member-facing UI to mark
   attending/not-attending per class is the Pass 3 portal, on schedule).

### Schedule follow-ups — RESOLVED (client, 2026-07-24)

- Groups A and B: **same age range** (mixed 7–17, one Youth Boxing type). ✔
- Billing: **$189 per 4-week cycle** (Stripe interval=week, count=4 in
  Pass 2). Disclosure updated to "billed every 4 weeks" — the §6 wording said
  "/month", which would be inaccurate at 13 cycles/year; disclosure accuracy
  is a card-network trial-rule requirement. Flag if wording must change back.
- **Auto-renews** each 4-week cycle, cancel anytime (no contracts). ✔
- **No group swaps** — members book only their own group's days. Implemented
  as `POLICY_ALLOW_CROSS_GROUP=0` config; the Pass 3 portal booking rules
  enforce it (trial bookings via the funnel are unaffected — a prospect picks
  either group for their free class, which effectively chooses their group).
- Still open: per-group capacity cap (currently 12).


## Decisions made (flagging per "ask before assuming")

1. **Membership price seed**: brief says "$180/month placeholder"; the earlier
   funnel build was corrected by you to **$189** on 2026-07-14, so the seed
   uses $189 (Unlimited, `is_placeholder=True`). Pricing is per-plan in the
   `plans` table, never hardcoded, and the no-charge disclosure derives from
   the plan. **Question: confirm adult rate and the youth rate** (one
   subscription per enrolled child bills the guardian's card in Pass 2).
2. **Referenced attachments**: `Box2fit_Platform_v1_Brief.md`,
   `Box2fit_Master_Implementation_Plan.md` and `Box2fit_Platform_Design_Brief.md`
   were not found on disk or in the zip — I built from the two briefs pasted in
   chat plus the "Box2Fit Health — Design System" zip (its platform UI kit:
   `platform.css`, BookingFlow states, Active Adults landing). If the .md files
   differ from the pasted text, send them and I'll reconcile.
3. **Design register**: per the platform UI-kit README, the product leads with
   the bold white/ink/crimson Box2Fit spine (`platform.css` tokens), not the
   sandy proposal palette. The youth landing follows the kit's landing
   template structure; photography from `assets/photos/youth-*`.
4. **No-charge disclosure wording**: the earlier funnel's verbatim disclosure
   named a fixed date and price. With per-plan pricing and the child path, it
   now reads: "No charge today. The {plan} membership ($X/month) starts only
   after {child}'s free class, and only if you choose to continue. We'll
   remind you before any charge. Cancel anytime in one click." Flag if the
   original wording must be kept verbatim.
5. **Waiver text**: placeholder minor + adult waiver documents (versioned
   table, re-sign flow ready). Real legal text needed before launch.
6. **Youth landing brackets**: 7–10 / 11–14 / 15–17 per the kickoff; booking
   validates the child's birth-year age against the bracket and bounces
   mismatches back to class pick with a friendly error.
7. **Trainer certifications** seeded as placeholders (NCCP/CPR etc.) on three
   placeholder coaches, per the seed spec.
8. **Age check granularity**: birth year only (PIPEDA minimum) means age is
   computed by calendar year. A child turning 11 in December can book Kids
   7–10 in January–December of that year or not, depending on year math —
   staff can override by booking manually. Flag if you want birth month.

## Open questions for the client/agency

- Youth membership rate (and whether Kids/Youth/Teen differ).
- Real weekly timetable + trainer roster (seed is placeholder, one swap file).
- Full waiver + PAR-Q legal text (minor and adult versions).
- Google review proof: reusing the 13 verbatim quotes + 5.0/28 badge from the
  verified profile export. Confirm still current.
- Stripe test-mode keys when ready (card step shows dev-skip until then).

## Not in Pass 1 (per brief)

Money lifecycle (Pass 2), other five landing pages + portal + kiosk (Pass 3),
schedule-builder UI / trainer mgmt / reporting / CAPI dispatch (Pass 4).
Schema for all of it is in migration zero.
