"""First-touch attribution: UTMs + landing variant in a 30-day cookie,
persisted to the lead at booking and mirrored into Stripe metadata."""
import json
from datetime import datetime, timezone

from flask import Request, Response, current_app

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def capture_first_touch(request: Request, response: Response, landing_variant: str) -> None:
    cookie_name = current_app.config["UTM_COOKIE_NAME"]
    if request.cookies.get(cookie_name):
        return  # first touch wins for 30 days
    utms = {k: request.args.get(k, "")[:255] for k in UTM_KEYS if request.args.get(k)}
    data = {
        **utms,
        "landing_variant": landing_variant,
        "first_touch_at": datetime.now(timezone.utc).isoformat(),
    }
    if request.args.get("ref"):
        data["referral_code"] = request.args.get("ref")[:20]
    response.set_cookie(
        cookie_name,
        json.dumps(data),
        max_age=current_app.config["UTM_COOKIE_MAX_AGE"],
        httponly=True,
        samesite="Lax",
        # Keyed to the actual scheme: https (prod) gets a Secure cookie; plain
        # http (local/LAN demos) still persists attribution.
        secure=request.is_secure,
    )


def read_first_touch(request: Request) -> dict:
    raw = request.cookies.get(current_app.config["UTM_COOKIE_NAME"])
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
