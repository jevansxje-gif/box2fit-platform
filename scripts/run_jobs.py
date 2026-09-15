"""All periodic jobs in one inline run — for servers without Redis/Celery.
Intended to be cron'd every 10 minutes:

    */10 * * * * cd /website/box2fit-platform && .venv/bin/python -m scripts.run_jobs >> /var/log/box2fit-jobs.log 2>&1

Runs: T-24h/T-2h class reminders, no-show automark, waitlist offer release,
tracking outbox drain, and (hourly, on the :0x run) schedule generation +
inactive-template pruning. Every job is idempotent, so overlap is harmless.
"""
from datetime import datetime

from app import create_app
from app.extensions import db
from app.models import ClientAccount
from app.services.class_admin import prune_inactive_template_instances
from app.services.scheduling import generate_instances
from app.services.tzutil import now_utc, utc_to_local
from app.tasks.jobs import (
    automark_no_shows,
    drain_event_outbox,
    payment_health_check,
    release_expired_waitlist_offers,
    send_due_reminders,
    send_trial_followups,
    weekly_ops_digest,
)

app = create_app()

with app.app_context():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    reminders = send_due_reminders.apply().get()
    noshows = automark_no_shows.apply().get()
    released = release_expired_waitlist_offers.apply().get()
    followups = send_trial_followups.apply().get()
    drained = drain_event_outbox.apply().get()

    generated = pruned = 0
    if datetime.now().minute < 10:  # top-of-hour run only
        for ca in db.session.query(ClientAccount).filter_by(active=True).all():
            generated += generate_instances(ca.id)
            pruned += prune_inactive_template_instances(ca.id)
        db.session.commit()

    # Daily 8am-local payment self-audit (alerts only on drift); the weekly
    # ops digest goes out Monday 8am. The <10 minute gate makes each fire on
    # a single cron tick per day.
    health = digest = "-"
    local = utc_to_local(now_utc())
    if local.hour == 8 and local.minute < 10:
        health = payment_health_check.apply().get()
        if local.weekday() == 0:  # Monday
            digest = weekly_ops_digest.apply().get()

    print(
        f"[{stamp}] reminders={reminders} noshows={noshows} "
        f"waitlist_released={released} followups={followups} outbox={drained} "
        f"generated={generated} pruned={pruned} health={health} digest={digest}"
    )
