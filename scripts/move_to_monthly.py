"""Move existing memberships from every 4 weeks to monthly on their start day
(owner 2026-10-19). Run the seed first so the plan is monthly. Dry run by
default; nothing changes in Stripe or the database without --apply.

    .venv/bin/python -m scripts.move_to_monthly
    .venv/bin/python -m scripts.move_to_monthly --apply --void-open

--void-open also cancels an unpaid (declined) charge so Stripe stops retrying
it; the member is then billed fresh on their start day.
"""
import logging
import sys

from app import create_app
from app.extensions import db
from app.models import Subscription, SubscriptionStatus, User
from app.services.billing import default_plan, move_subscription_to_monthly
from app.services.tzutil import fmt_local

apply = "--apply" in sys.argv
void_open = "--void-open" in sys.argv
app = create_app()
with app.app_context():
    import stripe

    stripe.api_version = "2019-09-09"
    logging.getLogger("stripe").setLevel(logging.WARNING)
    client_ids = {s.client_account_id for s in db.session.query(Subscription).all()}
    subs = []
    for cid in client_ids:
        plan = default_plan(cid)
        if plan is None:
            continue
        subs += (
            db.session.query(Subscription)
            .filter(
                Subscription.client_account_id == cid,
                Subscription.plan_id == plan.id,
                Subscription.challenge_key.is_(None),
                Subscription.status.in_([
                    SubscriptionStatus.pending.value,
                    SubscriptionStatus.active.value,
                    SubscriptionStatus.past_due.value,
                ]),
            )
            .all()
        )
    print(("APPLYING" if apply else "DRY RUN (add --apply to change anything)") + f" | {len(subs)} membership(s)")
    for sub in subs:
        who = db.session.get(User, sub.user_id)
        r = move_subscription_to_monthly(sub, apply=apply, void_open=void_open)
        line = (f"{who.name} | ${r['cents'] / 100:.2f} | {r['status']} | was {r.get('old_interval', '?')}"
                f" | start day {r['start_day']} | next charge {fmt_local(r['next_charge'], '%a %b %d, %I:%M %p')}")
        if r["voided"]:
            line += " | void " + ", ".join(f"{v['id']} (${(v['amount'] or 0) / 100:.2f})" for v in r["voided"])
        if r.get("skip"):
            line += f" | SKIPPED: {r['skip']}"
        elif r["applied"]:
            line += " | DONE"
        print(line)
    if apply:
        db.session.commit()
    print("ON BOXING - MONTHLY " + ("APPLIED" if apply else "DRY RUN"))
