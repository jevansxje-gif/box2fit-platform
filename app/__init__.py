from flask import Flask

from .config import Config
from .extensions import csrf, db, limiter, login_manager, migrate
from .logging_setup import configure_logging


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "portal.login"
    login_manager.blueprint_login_views = {
        "ops": "ops.login",
        "ops_admin": "ops.login",
        "portal": "portal.login",
    }

    from . import models  # noqa: F401

    @app.before_request
    def _reset_login_cache():
        # Flask-Login caches the resolved user on `g`. When an app context
        # outlives a request (long-lived contexts in tests/scripts), that
        # cache leaks across requests — force per-request resolution.
        # No-op in production where every request gets a fresh `g`.
        from flask import g

        g.pop("_login_user", None)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(models.User, int(user_id))

    from .blueprints.funnel import bp as funnel_bp
    from .blueprints.portal import bp as portal_bp
    from .blueprints.ops import bp as ops_bp
    from .blueprints.ops_admin import bp as ops_admin_bp
    from .blueprints.api import bp as api_bp
    from .blueprints.agency import bp as agency_bp

    app.register_blueprint(funnel_bp)
    app.register_blueprint(portal_bp, url_prefix="/portal")
    app.register_blueprint(ops_bp, url_prefix="/ops")
    app.register_blueprint(ops_admin_bp, url_prefix="/ops")
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(agency_bp, url_prefix="/agency")

    csrf.exempt(api_bp)  # the API authenticates with JWT, not session cookies
    csrf.exempt(app.view_functions["funnel.stripe_webhook_root"])

    from .tasks import celery_init_app

    celery_init_app(app)

    from .services.tzutil import fmt_local

    @app.context_processor
    def inject_globals():
        return {
            "META_PIXEL_ID": app.config["META_PIXEL_ID"],
            "GA4_MEASUREMENT_ID": app.config["GA4_MEASUREMENT_ID"],
            "fmt_local": fmt_local,
        }

    return app
