"""Absolute URL building that works outside a request context (Celery tasks)."""
from flask import current_app, url_for


def absolute_url(endpoint: str, **values) -> str:
    base = current_app.config["SITE_BASE_URL"].rstrip("/")
    with current_app.test_request_context():
        path = url_for(endpoint, **values)
    return base + path
