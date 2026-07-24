"""Dev-config proof that a fresh UTM click-through persists to the lead over
plain http (Secure-cookie regression check). Run: python -m scripts._verify_utm
"""
import re

from app import create_app
from app.extensions import db
from app.models import Lead

app = create_app()
c = app.test_client()

c.get("/youth?utm_source=meta&utm_campaign=youth-launch&utm_content=youth-B")

r = c.get("/book/youth")
iid = re.search(rb'name="instance_id" value="(\d+)"', r.data).group(1).decode()
tok = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', r.data).group(1).decode()
c.post("/book/youth", data={"csrf_token": tok, "instance_id": iid})

r = c.get("/book/youth/details")
tok = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', r.data).group(1).decode()
c.post(
    "/book/youth/details",
    data={
        "csrf_token": tok,
        "attendee_kind": "child",
        "child_first_name": "Ari",
        "child_birth_year": "2017",
        "emergency_contact_name": "EC Person",
        "emergency_contact_phone": "604-555-0101",
        "guardian_name": "Utm Parent",
        "email": "utm.parent@example.com",
        "phone": "604-555-0199",
        "waiver_agree": "y",
        "signature": "Utm Parent",
    },
)

with app.app_context():
    lead = db.session.query(Lead).filter_by(email="utm.parent@example.com").one()
    print(
        "fresh-session lead utm:",
        lead.utm_source,
        lead.utm_campaign,
        lead.utm_content,
        "| variant:",
        lead.landing_variant,
    )
