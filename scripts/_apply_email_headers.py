"""One-off: swap the text-only email header for logo + suffix across all
templates, and space out the calendar buttons. UTF-8 safe."""
from pathlib import Path

OLD_TPL = (
    '<div style="background:#14161A;padding:18px 24px">'
    '<span style="color:#fff;font-weight:800;letter-spacing:1px">{label}</span></div>'
)
LOGO = (
    '<img src="https://health.box2fit.com/static/img/brand/logo-white.png" '
    'alt="Box2Fit" height="24" style="vertical-align:middle;border:0" />'
)


def new_header(suffix: str) -> str:
    return (
        '<div style="background:#14161A;padding:14px 24px">' + LOGO +
        '<span style="color:#ffffff;font-weight:800;letter-spacing:2px;'
        'font-size:12px;vertical-align:middle;padding-left:10px">'
        + suffix + "</span></div>"
    )


REPLACEMENTS = [
    (OLD_TPL.format(label="BOX2FIT WHITE ROCK"), new_header("WHITE ROCK")),
    (OLD_TPL.format(label="BOX2FIT &middot; STAFF ALERT"), new_header("STAFF ALERT")),
    (OLD_TPL.format(label="BOX2FIT · STAFF ALERT"), new_header("STAFF ALERT")),
    (OLD_TPL.format(label="BOX2FIT · COACH SCHEDULE"), new_header("COACH SCHEDULE")),
    # calendar buttons: breathing room when they wrap on mobile
    (
        'text-decoration:none;margin-right:8px">Add to Google Calendar</a>',
        'text-decoration:none;margin:0 10px 12px 0">Add to Google Calendar</a>',
    ),
]

count = 0
for p in Path("app/templates/emails").glob("*.html"):
    text = p.read_text(encoding="utf-8")
    orig = text
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    if text != orig:
        p.write_text(text, encoding="utf-8")
        count += 1
        print("updated:", p.name)
print("total:", count)
