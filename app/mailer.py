from __future__ import annotations

from typing import List, Dict, Optional, Any
import base64
import smtplib
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.orm import Session

from .models import Setting
from .security import decrypt

import hashlib
try:
    hashlib.new("md4")
except ValueError:  # OpenSSL 3 tidak menyediakan MD4 (dibutuhkan NTLM) -> pakai pycryptodome
    try:
        from Crypto.Hash import MD4 as _MD4
        _orig_new = hashlib.new

        def _new(name, data=b"", **kw):
            if name.lower() == "md4":
                return _MD4.new(data)
            return _orig_new(name, data, **kw)

        hashlib.new = _new
    except ImportError:
        pass  # login NTLM akan gagal sampai "pip install pycryptodome"

DEFAULT_SETTINGS = {
    "smtp_host": "", "smtp_port": "587", "smtp_security": "starttls",  # none | starttls | ssl
    "smtp_auth": "login",  # login (LOGIN/PLAIN) | ntlm
    "smtp_sender": "", "smtp_user": "", "smtp_password_enc": "", "retention_days": "30",
}


def get_settings(db: Session) -> dict:
    d = dict(DEFAULT_SETTINGS)
    for s in db.query(Setting).all():
        d[s.key] = s.value
    return d


def set_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def _ntlm_login(server: smtplib.SMTP, user: str, password: str):
    from ntlm_auth.ntlm import NtlmContext
    domain, sep, uname = user.partition("\\")
    if not sep:
        domain, uname = "", user
    ctx = NtlmContext(uname, password, domain=domain or None, ntlm_compatibility=3)
    code, resp = server.docmd("AUTH", "NTLM " + base64.b64encode(ctx.step()).decode())
    if code != 334:
        raise smtplib.SMTPAuthenticationError(code, resp)
    code, resp = server.docmd(base64.b64encode(ctx.step(base64.b64decode(resp))).decode())
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, resp)


def send_mail(cfg: dict, to: list, cc: list, subject: str, html: str, attachment: Optional[Path] = None):
    if not cfg.get("smtp_host"):
        raise ValueError("SMTP host belum diisi (menu Email / SMTP)")
    if not cfg.get("smtp_sender"):
        raise ValueError("Alamat pengirim (From) belum diisi")
    if not to:
        raise ValueError("Penerima (To) kosong")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["smtp_sender"], ", ".join(to), subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content("Email ini berformat HTML.")
    msg.add_alternative(html, subtype="html")
    if attachment:
        msg.add_attachment(
            attachment.read_bytes(), maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=attachment.name,
        )
    host, port, sec = cfg["smtp_host"], int(cfg["smtp_port"] or 587), cfg["smtp_security"]
    server = smtplib.SMTP_SSL(host, port, timeout=60) if sec == "ssl" else smtplib.SMTP(host, port, timeout=60)
    try:
        server.ehlo()
        if sec == "starttls":
            server.starttls()
            server.ehlo()
        user, pw = cfg.get("smtp_user", ""), decrypt(cfg.get("smtp_password_enc", ""))
        if user:
            if cfg.get("smtp_auth") == "ntlm":
                _ntlm_login(server, user, pw)
            else:
                server.login(user, pw)
        server.send_message(msg, from_addr=cfg["smtp_sender"], to_addrs=to + cc)
    finally:
        try:
            server.quit()
        except Exception:
            pass
