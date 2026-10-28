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


# Campaign tabs on the Marketing page. The October 2026 launch tags every
# link utm_campaign=families|guided|shehits and utm_content=<campaign>-<ad>
# (families-f1, guided-g4, shehits-s1); the August test used ?v=c1..c3 with
# utm_campaign = the landing slug. "picker"/"details" are the two steps
# between landing and a lead for that offer (POST = form submitted).
CAMPAIGNS = {
    # Since 2026-10-26 the adult pages (and kids ad F2) open the call-first
    # short form at /start/<offer>: opening it counts as "picker", sending
    # it as "details". `call_offer` ties the tab to its Call list rows.
    "families": {"label": "Families", "landings": ("/kids", "/youth"),
                 "picker": [("GET", ("/book/kids", "/book/youth", "/start/kids"))],
                 "details": [("GET", ("/book/kids/details", "/book/youth/details")),
                             ("POST", ("/start/kids",))],
                 "paid": False, "call_offer": "kids"},
    "guided": {"label": "Guided Boxing", "landings": ("/challenge", "/guided-boxing"),
               "picker": [("GET", ("/challenge/join", "/start/challenge"))],
               "details": [("POST", ("/challenge/join", "/start/challenge"))],
               "paid": True, "call_offer": "challenge"},
    "shehits": {"label": "She Hits", "landings": ("/shehits",),
                "picker": [("GET", ("/shehits/start", "/book/shehits", "/start/shehits"))],
                "details": [("POST", ("/shehits/start", "/book/shehits/details", "/start/shehits"))],
                "paid": True, "call_offer": "shehits"},
    "bootcamp": {"label": "Boot camp 7 pm", "landings": ("/bootcamp-7pm",),
                 "picker": [("GET", ("/start/bootcamp",))],
                 "details": [("POST", ("/start/bootcamp",))],
                 "paid": True, "call_offer": "bootcamp"},
    "rounds": {"label": "12 Rounds", "landings": ("/12-rounds",),
               "picker": [("GET", ("/start/rounds",))],
               "details": [("POST", ("/start/rounds",))],
               "paid": True, "call_offer": "rounds"},
    # August 2026 test, kept for the record
    "kids": {"label": "Kids (Aug)", "landings": ("/kids",),
             "picker": ("GET", ("/book/kids",)), "details": ("GET", ("/book/kids/details",)),
             "paid": False},
    "youth": {"label": "Youth (Aug)", "landings": ("/youth",),
              "picker": ("GET", ("/book/youth",)), "details": ("GET", ("/book/youth/details",)),
              "paid": False},
    "beast": {"label": "Beast (Aug)", "landings": ("/beast",),
              "picker": ("GET", ("/book/beast",)), "details": ("GET", ("/book/beast/details",)),
              "paid": False},
}
DEFAULT_CAMPAIGN = "families"
# Meta ad names use A1..A7 for the guided ads; their links say g1..g7.
_AD_PREFIX = {"g": "A"}
_NON_AD = ("gym-referral", "call-in")


def ad_label(utm_content: str | None, v: str | None = None) -> str:
    """families-f1 -> F1, guided-g4 -> A4, shehits-s1 -> S1; old ?v=c1 -> c1."""
    if utm_content in _NON_AD:
        return utm_content
    if utm_content and "-" in utm_content:
        code = utm_content.rsplit("-", 1)[1]
        if code[:1].isalpha() and code[1:].isdigit():
            return _AD_PREFIX.get(code[0], code[0].upper()) + code[1:]
        return code
    return v or utm_content or "unknown"


def _matches(method: str, path: str, step) -> bool:
    if isinstance(step, list):  # several (method, prefixes) pairs
        return any(_matches(method, path, s) for s in step)
    want, prefixes = step
    base = path.split("?", 1)[0]
    return method == want and any(base == p or base.startswith(p + "/") for p in prefixes)


def traffic_funnel(campaign: str = DEFAULT_CAMPAIGN) -> dict:
    """Per-human-visitor journeys for one campaign's ad traffic."""
    from urllib.parse import parse_qs, urlsplit

    cfg = CAMPAIGNS.get(campaign, CAMPAIGNS[DEFAULT_CAMPAIGN])
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
                    ip, req_path, ua, method = m["ip"], m["path"], m["ua"], m["method"]
                    if "fbclid=fbclid" in req_path:  # internal test links
                        continue
                    if _is_bot(ip, ua):
                        continue
                    parts = urlsplit(req_path)
                    q = parse_qs(parts.query)
                    on_landing = parts.path in cfg["landings"]
                    tagged = q.get("utm_source", [""])[0] == "meta" and (
                        q.get("utm_campaign", [campaign])[0] == campaign
                    )
                    # a meta-tagged landing starts/refreshes this visitor
                    if method == "GET" and on_landing and tagged:
                        v = visitors.setdefault(
                            ip, {"ad": None, "landings": 0, "picker": False,
                                 "details": False}
                        )
                        v["landings"] += 1
                        ad = ad_label(q.get("utm_content", [None])[0], q.get("v", [None])[0])
                        if ad != "unknown":
                            v["ad"] = ad
                    elif ip in visitors:
                        if _matches(method, req_path, cfg["details"]):
                            visitors[ip]["details"] = True
                            visitors[ip]["picker"] = True
                        elif _matches(method, req_path, cfg["picker"]):
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


def db_funnel(client_account_id: int, campaign: str = DEFAULT_CAMPAIGN) -> dict:
    """Database truth: leads with meta UTMs and how far each got. "paid" is a
    live Challenge / She Hits intro subscription on the lead's account."""
    from ..extensions import db
    from ..models import Lead, LeadStatus, Subscription
    from . import challenge as ch

    leads = (
        db.session.query(Lead)
        .filter_by(client_account_id=client_account_id, utm_source="meta")
        .filter(Lead.utm_campaign == campaign)
        .order_by(Lead.created_at.desc())
        .all()
    )
    paid_users = set()
    if CAMPAIGNS.get(campaign, {}).get("paid"):
        paid_users = {
            uid for (uid,) in db.session.query(Subscription.user_id).filter(
                Subscription.client_account_id == client_account_id,
                Subscription.challenge_key.in_(list(ch.OFFERS)),
                Subscription.status.in_(ch.LIVE),
            )
        }
    per_ad: dict[str, dict] = defaultdict(
        lambda: {"leads": 0, "booked": 0, "paid": 0, "attended": 0, "activated": 0}
    )
    recent = []
    for lead in leads:
        ad = ad_label(lead.utm_content)
        paid = lead.user_id in paid_users
        if len(recent) < 12:
            recent.append({"lead": lead, "ad": ad, "paid": paid})
        per_ad[ad]["leads"] += 1
        if lead.status in (
            LeadStatus.booked.value, LeadStatus.attended.value,
            LeadStatus.activated.value,
        ):
            per_ad[ad]["booked"] += 1
        if paid:
            per_ad[ad]["paid"] += 1
        if lead.status in (LeadStatus.attended.value, LeadStatus.activated.value):
            per_ad[ad]["attended"] += 1
        if lead.status == LeadStatus.activated.value:
            per_ad[ad]["activated"] += 1
    totals = {
        k: sum(a[k] for a in per_ad.values())
        for k in ("leads", "booked", "paid", "attended", "activated")
    }
    return {"per_ad": dict(sorted(per_ad.items())), "totals": totals,
            "recent": recent}


def call_first_funnel(client_account_id: int, campaign: str = DEFAULT_CAMPAIGN) -> dict | None:
    """The call-first short forms for this tab's offer, per ad: forms sent,
    called (any status but new), agreed (link sent or signed up) and the
    real outcome (paid / booked) read from the linked account."""
    from ..extensions import db
    from ..models import Enquiry
    from . import enquiry as enq_svc

    offer = CAMPAIGNS.get(campaign, {}).get("call_offer")
    if not offer:
        return None
    rows = (
        db.session.query(Enquiry)
        .filter_by(client_account_id=client_account_id, offer=offer)
        .order_by(Enquiry.created_at.desc())
        .all()
    )
    per_ad: dict[str, dict] = defaultdict(lambda: {"forms": 0, "called": 0, "agreed": 0, "won": 0})
    recent = []
    for e in rows:
        ad = ad_label(e.utm_content) if e.utm_content else "direct"
        won = enq_svc.outcome(e)
        a = per_ad[ad]
        a["forms"] += 1
        a["called"] += 1 if e.status != "new" or won else 0
        a["agreed"] += 1 if (e.status == "agreed" or e.link_sent_at or e.booked_for) else 0
        a["won"] += 1 if won else 0
        if len(recent) < 12:
            recent.append({"e": e, "ad": ad, "won": won})
    totals = {k: sum(a[k] for a in per_ad.values()) for k in ("forms", "called", "agreed", "won")}
    return {"per_ad": dict(sorted(per_ad.items())), "totals": totals, "recent": recent,
            "won_label": "Booked" if offer == "kids" else "Paid"}
