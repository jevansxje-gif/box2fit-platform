"""Celery entrypoint:
  celery -A celery_worker worker -l info --pool=solo   (Windows dev)
  celery -A celery_worker beat -l info
"""
from app import create_app

flask_app = create_app()
celery = flask_app.extensions["celery"]
