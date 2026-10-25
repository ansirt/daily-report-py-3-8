from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

TZ_NAME = os.getenv("APP_TIMEZONE", "Asia/Jakarta")
TZ = ZoneInfo(TZ_NAME)


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v)


def split_emails(s):
    return [x.strip() for x in re.split(r"[,;\n]+", s or "") if x.strip()]


def render(tpl: str, ctx: dict) -> str:
    for k, v in ctx.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "report"
