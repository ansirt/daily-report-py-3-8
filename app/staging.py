from __future__ import annotations

"""Simpan hasil query ke tabel staging di database tujuan (untuk data besar)."""
import re
from datetime import date, datetime
from decimal import Decimal

from .dbconnect import open_connection

_TABLE = re.compile(r"^STG_[A-Z0-9_]{1,26}$")
ORACLE_STR_MAX = 1000


def check_table(name) -> str:
    n = (name or "").strip().upper()
    if not _TABLE.match(n):
        raise ValueError("Nama tabel staging harus diawali STG_ (huruf/angka/_, maksimal 30 karakter). "
                         "Awalan ini mencegah aplikasi menimpa tabel asli.")
    return n


def _colnames(cols):
    out, seen = [], set()
    for i, c in enumerate(cols):
        n = re.sub(r"[^A-Za-z0-9_]", "_", str(c)).upper()[:30] or f"COL{i + 1}"
        if not re.match(r"[A-Z_]", n[0]):
            n = ("C_" + n)[:30]
        base, k = n, 2
        while n in seen:
            suf = f"_{k}"
            n, k = base[:30 - len(suf)] + suf, k + 1
        seen.add(n)
        out.append(n)
    return out


def _kind(values):
    ks = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, (bool, int)):
            ks.add("int")
        elif isinstance(v, (float, Decimal)):
            ks.add("num")
        elif isinstance(v, datetime):
            ks.add("ts")
        elif isinstance(v, date):
            ks.add("date")
        else:
            ks.add("str")
    if not ks or "str" in ks:
        return "str"
    if ks <= {"int"}:
        return "int"
    if ks <= {"int", "num"}:
        return "num"
    return "date" if ks == {"date"} else "ts" if ks <= {"ts", "date"} else "str"


def _coltype(db, kind, maxlen):
    n = max(maxlen, 1)
    if db == "oracle":
        return {"int": "NUMBER(19)", "num": "NUMBER", "ts": "TIMESTAMP", "date": "DATE"}.get(
            kind, f"VARCHAR2({min(n, ORACLE_STR_MAX)} CHAR)")
    if db == "mysql":
        return {"int": "BIGINT", "num": "DECIMAL(38,10)", "ts": "DATETIME(6)", "date": "DATE"}.get(
            kind, f"VARCHAR({n})" if n <= 255 else "TEXT")
    return {"int": "BIGINT", "num": "DECIMAL(38,10)", "ts": "DATETIME2", "date": "DATE"}.get(
        kind, f"NVARCHAR({n})" if n <= 4000 else "NVARCHAR(MAX)")


def _quote(db, n):
    return f'"{n}"' if db == "oracle" else f"`{n}`" if db == "mysql" else f"[{n}]"


def stage_table(info, table, cols, rows):
    """Buat ulang tabel (DROP + CREATE) lalu isi. Mengembalikan (nama_tabel, [peringatan])."""
    table, db, warns = check_table(table), info.db_type, []
    names = _colnames(cols)
    kinds = [_kind(r[j] for r in rows) for j in range(len(names))]
    lens = [max([len(str(r[j])) for r in rows if r[j] is not None] or [1]) for j in range(len(names))]
    defs = [f"{_quote(db, n)} {_coltype(db, k, max(l, 1) if rows else 255)}" for n, k, l in zip(names, kinds, lens)]

    def conv(v, k):
        if v is None:
            return None
        if k == "str":
            s = v if isinstance(v, str) else str(v)
            return s
        return int(v) if k == "int" else v

    data, cut = [], 0
    for r in rows:
        row = [conv(r[j], kinds[j]) for j in range(len(names))]
        if db == "oracle":
            for j, k in enumerate(kinds):
                if k == "str" and row[j] is not None and len(row[j]) > ORACLE_STR_MAX:
                    row[j], cut = row[j][:ORACLE_STR_MAX], cut + 1
        data.append(tuple(row))
    if cut:
        warns.append(f"{cut} nilai teks dipotong ke {ORACLE_STR_MAX} karakter (batas kolom Oracle)")

    ph = ",".join(f":{i + 1}" for i in range(len(names))) if db == "oracle" else ",".join(["%s"] * len(names))
    insert = f"INSERT INTO {table} ({','.join(_quote(db, n) for n in names)}) VALUES ({ph})"
    conn = open_connection(info)
    try:
        cur = conn.cursor()
        if db == "oracle":
            try:
                cur.execute(f"DROP TABLE {table} PURGE")
            except Exception as e:
                if "ORA-00942" not in str(e):
                    raise
        elif db == "mysql":
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        else:
            cur.execute(f"IF OBJECT_ID('{table}','U') IS NOT NULL DROP TABLE {table}")
        cur.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
        for i in range(0, len(data), 1000):
            cur.executemany(insert, data[i:i + 1000])
        conn.commit()
    finally:
        conn.close()
    return table, warns
