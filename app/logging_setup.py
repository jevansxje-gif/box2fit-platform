"""Structured JSON logging + optional Sentry hook."""
import logging
import sys


def configure_logging(app) -> None:
    if app.testing:
        return
    handler = logging.StreamHandler(sys.stdout)
    try:
        from pythonjsonlogger.json import JsonFormatter
    except ImportError:  # python-json-logger < 3
        from pythonjsonlogger.jsonlogger import JsonFormatter
    handler.setFormatter(
        JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    dsn = app.config.get("SENTRY_DSN")
    if dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(dsn=dsn, traces_sample_rate=0.05)
        except ImportError:
            app.logger.warning("SENTRY_DSN set but sentry-sdk not installed")
