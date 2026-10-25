from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
OUTPUT_DIR = DATA_DIR / "output"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"
TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "12"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"  # set 1 if served over HTTPS
ORACLE_CLIENT_LIB_DIR = os.getenv("ORACLE_CLIENT_LIB_DIR")  # only for Oracle < 12.1 (thick mode)


def _load_secret() -> str:
    env = os.getenv("APP_SECRET_KEY")
    if env:
        return env
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
    return f.read_text().strip()


SECRET_KEY = _load_secret()

CSV_DIR = Path(os.getenv("CSV_DIR", DATA_DIR / "csv"))  # file CSV (upload & hasil export node Export CSV)
CSV_DIR.mkdir(parents=True, exist_ok=True)
