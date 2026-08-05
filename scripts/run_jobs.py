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
from app.tasks.jobs import (
    automark_no_shows,
    drain_event_outbox,
    release_expired_waitlist_offers,
    send_due_reminders,
)

app = create_app()

with app.app_context():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    reminders = send_due_reminders.apply().get()
    noshows = automark_no_shows.apply().get()
    released = release_expired_waitlist_offers.apply().get()
    drained = drain_event_outbox.apply().get()

    generated = pruned = 0
    if datetime.now().minute < 10:  # top-of-hour run only
        for ca in db.session.query(ClientAccount).filter_by(active=True).all():
            generated += generate_instances(ca.id)
            pruned += prune_inactive_template_instances(ca.id)
        db.session.commit()

    print(
        f"[{stamp}] reminders={reminders} noshows={noshows} "
        f"waitlist_released={released} outbox={drained} "
        f"generated={generated} pruned={pruned}"
    )
