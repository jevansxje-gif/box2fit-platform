"""Marketing funnel report: ad click → landing → picker → details form
(from nginx access logs, Meta's bot fleet filtered out) joined with the
database truth (leads → booked → attended → activated, by utm_content).

Log access degrades gracefully: if the files are unreadable or absent
(dev machines), the traffic section reports why and the DB funnel still
renders. Spend is entered manually from Ads Manager (no Meta API creds).
"""
import os
import re
from collections import defaultdict

# Meta/Facebook infrastructure prefixes (ad review, link preview, prefetch
# fleets) plus common cloud scanners — traffic from these is not a person.
BOT_IP_PREFIXES = (
    "31.13.", "57.141.", "66.220.", "69.63.", "69.171.", "102.132.",
    "129.134.", "157.240.", "163.70.", "173.252.", "179.60.", "185.60.",
    "204.15.20.", "34.", "35.", "52.", "54.", "3.",
)
BOT_UA_MARKERS = (
    "facebookexternalhit", "meta-external", "bot", "crawler", "spider",
    "preview", "python-requests", "curl",
)

# combined log format: ip - - [time] "METHOD path HTTP/x" status size "ref" "ua"
_LINE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<method>\S+) '
    r'(?P<path>\S+)[^"]*" (?P<status>\d{3}) \S+ "(?P<ref>[^"]*)" "(?P<ua>[^"]*)"'
)


def _log_paths() -> list[str]:
    base = os.environ.get("NGINX_ACCESS_LOG", "/var/log/nginx/box2fit.access.log")
    return [base + ".1", base]


def _is_bot(ip: str, ua: str) -> bool:
    ua_l = ua.lower()
    return ip.startswith(BOT_IP_PREFIXES) or any(m in ua_l for m in BOT_UA_MARKERS)


def traffic_funnel(campaign: str = "kids") -> dict:
    """Per-human-visitor journeys for one campaign's ad traffic."""
    landing_path = f"/{campaign}"
    visitors: dict[str, dict] = {}
    lines_read = 0
    error = None

    for path in _log_paths():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    m = _LINE.match(raw)
                    if not m:
                        continue
                    lines_read += 1
                    ip, req_path, ua = m["ip"], m["path"], m["ua"]
                    if "fbclid=fbclid" in req_path:  # internal test links
                        continue
                    if _is_bot(ip, ua):
                        continue
                    # a meta-tagged landing starts/refreshes this visitor
                    if req_path.startswith(landing_path) and "utm_source=meta" in req_path:
                        ad = re.search(r"[?&]v=(c\d)", req_path)
                        v = visitors.setdefault(
                            ip, {"ad": None, "landings": 0, "picker": False,
                                 "details": False}
                        )
                        v["landings"] += 1
                        if ad:
                            v["ad"] = ad.group(1)
                    elif ip in visitors:
                        if req_path.startswith(f"/book/{campaign}/details"):
                            visitors[ip]["details"] = True
                            visitors[ip]["picker"] = True
                        elif req_path.startswith(f"/book/{campaign}"):
                            visitors[ip]["picker"] = True
        except FileNotFoundError:
            continue
        except PermissionError:
            error = f"no permission to read {path}"

    if lines_read == 0 and error is None:
        error = "access log not found on this machine"

    per_ad: dict[str, dict] = defaultdict(
        lambda: {"visitors": 0, "landings": 0, "picker": 0, "details": 0}
    )
    for v in visitors.values():
        ad = v["ad"] or "unknown"
        per_ad[ad]["visitors"] += 1
        per_ad[ad]["landings"] += v["landings"]
        per_ad[ad]["picker"] += 1 if v["picker"] else 0
        per_ad[ad]["details"] += 1 if v["details"] else 0

    totals = {
        "visitors": len(visitors),
        "landings": sum(v["landings"] for v in visitors.values()),
        "picker": sum(1 for v in visitors.values() if v["picker"]),
        "details": sum(1 for v in visitors.values() if v["details"]),
    }
    return {"per_ad": dict(sorted(per_ad.items())), "totals": totals,
            "error": error}


def db_funnel(client_account_id: int, campaign: str = "kids") -> dict:
    """Database truth: leads with meta UTMs and how far each got."""
    from ..extensions import db
    from ..models import Lead, LeadStatus

    leads = (
        db.session.query(Lead)
        .filter_by(client_account_id=client_account_id, utm_source="meta")
        .filter(Lead.utm_campaign == campaign)
        .order_by(Lead.created_at.desc())
        .all()
    )
    per_ad: dict[str, dict] = defaultdict(
        lambda: {"leads": 0, "booked": 0, "attended": 0, "activated": 0}
    )
    for lead in leads:
        ad = lead.utm_content or "unknown"
        per_ad[ad]["leads"] += 1
        if lead.status in (
            LeadStatus.booked.value, LeadStatus.attended.value,
            LeadStatus.activated.value,
        ):
            per_ad[ad]["booked"] += 1
        if lead.status in (LeadStatus.attended.value, LeadStatus.activated.value):
            per_ad[ad]["attended"] += 1
        if lead.status == LeadStatus.activated.value:
            per_ad[ad]["activated"] += 1
    totals = {
        k: sum(a[k] for a in per_ad.values())
        for k in ("leads", "booked", "attended", "activated")
    }
    return {"per_ad": dict(sorted(per_ad.items())), "totals": totals,
            "recent": leads[:12]}
