from __future__ import annotations

from typing import Any, Dict, List, Optional
"""Koneksi multi-database (Oracle, MySQL, SQL Server) -> (columns, rows)."""
import re
from types import SimpleNamespace

from .config import ORACLE_CLIENT_LIB_DIR
from .security import decrypt

DEFAULT_PORTS = {"oracle": 1521, "mysql": 3306, "mssql": 1433}
_oracle_ready = False


def conn_info(row) -> SimpleNamespace:
    return SimpleNamespace(
        db_type=row.db_type, host=row.host, port=row.port or DEFAULT_PORTS.get(row.db_type),
        database=row.database, username=row.username, password=decrypt(row.password_enc), dsn=row.dsn,
    )


def validate_readonly(sql: str) -> str:
    """Laporan hanya boleh SELECT / WITH, satu statement. Mengembalikan SQL tanpa ';' di akhir."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"'(?:[^']|'')*'", "''", s).strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("Hanya satu statement yang diperbolehkan")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("Query harus diawali SELECT atau WITH")
    return sql.strip().rstrip(";").rstrip()


def open_connection(c, stream=False):
    if c.db_type == "oracle":
        global _oracle_ready
        import oracledb
        if not _oracle_ready:
            oracledb.defaults.fetch_lobs = False  # CLOB -> str
            if ORACLE_CLIENT_LIB_DIR:
                oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_LIB_DIR)
            _oracle_ready = True
        dsn = c.dsn or oracledb.makedsn(c.host, c.port, service_name=c.database)
        return oracledb.connect(user=c.username, password=c.password, dsn=dsn)
    if c.db_type == "mysql":
        import pymysql
        return pymysql.connect(host=c.host, port=c.port, user=c.username, password=c.password,
                               database=c.database or None, charset="utf8mb4", connect_timeout=15,
                               cursorclass=pymysql.cursors.SSCursor if stream else pymysql.cursors.Cursor)
    if c.db_type == "mssql":
        import pymssql
        return pymssql.connect(server=c.host, port=str(c.port), user=c.username, password=c.password,
                               database=c.database or "", login_timeout=15)
    raise ValueError(f"db_type tidak dikenal: {c.db_type}")


def run_query(c, sql: str, limit: Optional[int] = None):
    sql = validate_readonly(sql)
    conn = open_connection(c)
    try:
        cur = conn.cursor()
        if c.db_type == "oracle":
            cur.arraysize = 5000
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
        return cols, [list(r) for r in rows]
    finally:
        conn.close()


def test_connection(c) -> str:
    conn = open_connection(c)
    try:
        cur = conn.cursor()
        cur.execute("select 1 from dual" if c.db_type == "oracle" else "select 1")
        cur.fetchall()
        return "ok"
    finally:
        conn.close()
