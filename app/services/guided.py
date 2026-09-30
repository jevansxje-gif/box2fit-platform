"""Guided Start: the five taps a new adult answers when they sign up, so the
coach isn't starting from zero. Stored on the attendee (health_json["guided"]),
summarised for the member page, the day list and the staff alert."""

GOALS = [
    ("fitter", "Get fitter"), ("leaner", "Feel leaner"), ("box", "Learn to box properly"),
    ("stress", "Stress and headspace"), ("stronger", "Get stronger"), ("back", "Back into training after a break"),
    ("mine", "Time that's mine"),
]
LEVELS = [
    ("none", "I don't exercise at the moment"), ("bit", "A bit, now and then"),
    ("weekly", "1 to 2 times a week"), ("often", "3 or more a week"),
]
BEFORE = [
    ("what", "Not knowing what to do"), ("watched", "Feeling watched"), ("stick", "Couldn't stick with it"),
    ("time", "No time"), ("cost", "Cost"), ("never", "Never tried one"), ("like", "Nothing, I like gyms"),
]
FLAGS = [
    ("injury", "An injury or pain right now"), ("condition", "A condition or medication that affects exercise"),
    ("pregnant", "Pregnant or recently had a baby"), ("nervous", "I'm nervous about this"),
]
TIMES = [
    ("early", "Early morning (before 7)"), ("morning", "Morning (7 to 9)"), ("midmorning", "Mid-morning (9 to 11)"),
    ("lunch", "Lunchtime (11 to 1)"), ("afternoon", "Afternoon (1 to 4)"), ("evening", "After work (5 to 7)"),
    ("late", "Later evening (7 to 9)"), ("weekend", "Weekend morning"),
]
GUIDED = {"goals": GOALS, "levels": LEVELS, "before": BEFORE, "flags": FLAGS, "times": TIMES}
_L = {k: dict(v) for k, v in GUIDED.items()}


def parse(form) -> dict | None:
    """Read the questionnaire from a submitted form; None if untouched."""
    def picks(name, table):
        valid = _L[table]
        return [v for v in form.getlist(name) if v in valid]

    g = {
        "goals": picks("g_goals", "goals"),
        "level": form.get("g_level") if form.get("g_level") in _L["levels"] else None,
        "before": picks("g_before", "before"),
        "flags": picks("g_flags", "flags"),
        "times": picks("g_times", "times"),
        "success": (form.get("g_success") or "").strip()[:200] or None,
        "notes": (form.get("g_notes") or "").strip()[:300] or None,
    }
    if not any([g["goals"], g["level"], g["before"], g["flags"], g["times"], g["success"], g["notes"]]):
        return None
    return g


def summary(g: dict | None) -> str:
    """One line for the coach: 'wants: fitter, stress · starts: none · put off by: watched · nervous · knee'."""
    if not g:
        return ""
    bits = []
    if g.get("goals"):
        bits.append("wants: " + ", ".join(_L["goals"].get(k, k).lower() for k in g["goals"]))
    if g.get("level"):
        bits.append("now: " + _L["levels"].get(g["level"], g["level"]).lower())
    if g.get("before"):
        bits.append("put off by: " + ", ".join(_L["before"].get(k, k).lower() for k in g["before"]))
    if g.get("flags"):
        bits.append(", ".join(_L["flags"].get(k, k).lower() for k in g["flags"]))
    if g.get("notes"):
        bits.append(g["notes"])
    if g.get("success"):
        bits.append(f'"{g["success"]}"')
    return " · ".join(bits)


def url_for_attendee(attendee_id: int) -> str:
    """Signed link to the questionnaire page (no login; 90 days)."""
    from .signed_links import SALT_GUIDED, make_token
    from .urls import absolute_url

    return absolute_url("funnel.guided_start", token=make_token(attendee_id, SALT_GUIDED))
