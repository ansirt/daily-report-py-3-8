from __future__ import annotations

"""Membuat tabel database aplikasi dan user admin.

    py seed.py                                  # tanya password
    py seed.py --username admin --password RAHASIA123
    py seed.py --reset --password BARU12345     # ganti password user yang sudah ada
Aman dijalankan ulang: tabel yang ada tidak diubah, user yang ada tidak ditimpa (kecuali --reset).
"""
import argparse
import getpass
import os
import secrets

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import User
from app.security import hash_password


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "admin"))
    ap.add_argument("--password", default=os.getenv("ADMIN_PASSWORD"))
    ap.add_argument("--reset", action="store_true", help="ganti password jika user sudah ada")
    a = ap.parse_args()

    init_db()
    print("Tabel siap.")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == a.username))
        if user and not a.reset:
            print(f"User '{a.username}' sudah ada. Pakai --reset untuk mengganti passwordnya.")
            return
        pw, generated = a.password, False
        if not pw:
            pw = getpass.getpass("Password (min. 8 karakter, kosongkan untuk dibuat acak): ")
            if not pw:
                pw, generated = secrets.token_urlsafe(9), True
        if len(pw) < 8:
            raise SystemExit("Password minimal 8 karakter.")
        if user:
            user.password_hash = hash_password(pw)
        else:
            db.add(User(username=a.username, password_hash=hash_password(pw)))
        db.commit()
        print(f"User '{a.username}' {'diperbarui' if user else 'dibuat'}." + (f" Password: {pw}" if generated else ""))


if __name__ == "__main__":
    main()
