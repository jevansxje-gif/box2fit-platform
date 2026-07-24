"""All schedule math in America/Vancouver; all storage naive-UTC."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from ..config import LOCAL_TZ

TZ = ZoneInfo(LOCAL_TZ)


def local_to_utc(d: date, t: time) -> datetime:
    """Naive-UTC datetime for a local wall-clock date+time."""
    return (
        datetime.combine(d, t, tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    )


def utc_to_local(dt_utc: datetime) -> datetime:
    """Aware local datetime from a stored naive-UTC datetime."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_local() -> date:
    return datetime.now(TZ).date()


def fmt_local(dt_utc: datetime, fmt: str = "%A %b %d at %I:%M %p") -> str:
    s = utc_to_local(dt_utc).strftime(fmt)
    return s.replace(" 0", " ").lstrip("0")
