"""Celery wired to the Flask app context. Beat: reminders every 10 min,
schedule generation nightly, no-show automark every 15 min."""
from celery import Celery, Task
from celery.schedules import crontab


def celery_init_app(app) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name, task_cls=FlaskTask)
    celery_app.conf.update(
        broker_url=app.config["REDIS_URL"],
        result_backend=app.config["REDIS_URL"],
        task_always_eager=app.config["CELERY_TASK_ALWAYS_EAGER"],
        task_ignore_result=True,
        timezone="America/Vancouver",
        beat_schedule={
            "booking-reminders": {
                "task": "app.tasks.jobs.send_due_reminders",
                "schedule": crontab(minute="*/10"),
            },
            "generate-schedule": {
                "task": "app.tasks.jobs.generate_all_schedules",
                "schedule": crontab(hour=3, minute=15),
            },
            "noshow-automark": {
                "task": "app.tasks.jobs.automark_no_shows",
                "schedule": crontab(minute="*/15"),
            },
            "waitlist-release": {
                "task": "app.tasks.jobs.release_expired_waitlist_offers",
                "schedule": crontab(minute="*/10"),
            },
            "outbox-dispatch": {
                "task": "app.tasks.jobs.drain_event_outbox",
                "schedule": crontab(minute="*/2"),
            },
            "call-matching": {
                "task": "app.tasks.jobs.match_unmatched_calls",
                "schedule": crontab(minute="*/15"),
            },
        },
    )
    celery_app.set_default()
    app.extensions["celery"] = celery_app

    from . import jobs  # noqa: F401  register tasks

    return celery_app
