from __future__ import annotations

from typing import List, Dict, Optional, Any
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet
from fastapi import Cookie, HTTPException

from .config import SECRET_KEY, TOKEN_HOURS

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
_ITER = 310_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _ITER)
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode() if text else ""


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode() if token else ""


def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm="HS256")


def require_user(session: Optional[str] = Cookie(default=None)) -> str:
    if not session:
        raise HTTPException(401, "Belum login")
    try:
        return jwt.decode(session, SECRET_KEY, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Sesi berakhir, silakan login ulang")
