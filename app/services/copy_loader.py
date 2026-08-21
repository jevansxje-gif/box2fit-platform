"""Landing-page copy in content/copy/<slug>.yaml — text swaps without
deploys. Cached per-process, re-read on mtime change."""
import time
from pathlib import Path

import yaml

COPY_DIR = Path(__file__).resolve().parent.parent.parent / "content" / "copy"

# slug -> class segment_tag the booking flow filters on. /kids has its own
# custom page; retired slugs (reset/focus/strong) 301 in the funnel blueprint.
SEGMENT_ROUTES = {
    "youth": "youth",          # 7pm confidence class, ages 11-18
    "technical": "technical",  # 6pm learn-to-box
    "bootcamp": "bootcamp",    # 5pm conditioning
    "shehits": "shehits",      # 10am women-only
    "beast": "beast",          # 6am strength & conditioning
}

_cache: dict[str, tuple[float, float, dict]] = {}
_CACHE_TTL = 30


def load_copy(slug: str) -> dict | None:
    if slug not in SEGMENT_ROUTES:
        return None
    path = COPY_DIR / f"{slug}.yaml"
    if not path.exists():
        return None
    now = time.monotonic()
    cached = _cache.get(slug)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[2]
    mtime = path.stat().st_mtime
    if cached and cached[1] == mtime:
        _cache[slug] = (now, mtime, cached[2])
        return cached[2]
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["slug"] = slug
    data["book_segment"] = SEGMENT_ROUTES[slug]
    _cache[slug] = (now, mtime, data)
    return data
