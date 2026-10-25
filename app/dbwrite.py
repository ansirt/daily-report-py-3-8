from __future__ import annotations

"""Node 'Tulis ke database': insert / update / upsert / SQL bebas (nilai selalu lewat bind parameter)."""
import re

from .derived import _idx

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*(\.[A-Za-z_][A-Za-z0-9_$#]*)?$")
MODES = ("insert", "update", "upsert", "custom")
DEFAULT_MAX_ROWS = 10000
_LIT = re.compile(r"('(?:[^']|'')*')")


def parse_mapping(text):
    """'kolom_tabel=kolom_data, kolom2' -> [(kolom_tabel, kolom_data)] (tanpa '=' = nama sama)."""
    out = []
    for it in re.split(r"[,;\n]+", text or ""):
        it = it.strip()
        if not it:
            continue
        t, _, d = it.partition("=")
        t = t.strip()
        d = d.strip() or t
        if not IDENT.match(t):
            raise ValueError(f"Nama kolom tabel tidak valid: '{t}'")
        out.append((t, d))
    return out


def _strip_comments(sql):
    parts = _LIT.split(sql)
    return "".join(p if i % 2 else re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", " ", p, flags=re.S))
                   for i, p in enumerate(parts))


def validate_write_sql(sql):
    s = _strip_comments(sql or "")
    body = _LIT.sub("''", s).strip().rstrip(";").strip()
    if ";" in body:
        raise ValueError("Hanya satu statement yang diperbolehkan")
    if not re.match(r"(?is)^(insert|update|merge)\b", body):
        raise ValueError("SQL bebas harus diawali INSERT, UPDATE atau MERGE")
    return s.strip().rstrip(";").rstrip()


def validate_dbwrite(cfg, sql=""):
    mode = cfg.get("mode")
    if mode not in MODES:
        raise ValueError("Mode tulis belum dipilih")
    if cfg.get("source") in (None, ""):
        raise ValueError("Data sumber belum dipilih")
    if not cfg.get("target_connection_id"):
        raise ValueError("Database tujuan belum dipilih")
    if mode == "custom":
        validate_write_sql(sql)
        return
    if not TABLE.match(cfg.get("table") or ""):
        raise ValueError("Nama tabel tujuan tidak valid (contoh: schema.nama_tabel)")
    if not parse_mapping(cfg.get("set")):
        raise ValueError("Kolom yang diisi/diubah belum diisi")
    if mode in ("update", "upsert") and not parse_mapping(cfg.get("keys")):
        raise ValueError("Kolom kunci wajib diisi (mencegah UPDATE ke semua baris)")


def compile_named(sql, db):
    """':nama' -> placeholder driver. Oracle: tetap named (dict); MySQL/MSSQL: %s berurutan."""
    names, out = [], []
    pat = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")

    def rep(m):
        n = m.group(1)
        if db == "oracle":
            if n not in names:
                names.append(n)
            return m.group(0)
        names.append(n)
        return "%s"

    for i, p in enumerate(_LIT.split(sql)):
        if db != "oracle":
            p = p.replace("%", "%%")
        out.append(pat.sub(rep, p) if i % 2 == 0 else p)
    return "".join(out), names


def prepare_write(db, cfg, sql, cols):
    """Mengembalikan (statement, fungsi_baris->parameter)."""
    mode = cfg["mode"]
    if mode == "custom":
        stmt, names = compile_named(validate_write_sql(sql), db)
        idx = {n: _idx(cols, n) for n in set(names)}
        if db == "mssql" and re.match(r"(?is)^\s*merge\b", stmt):
            stmt += ";"
        if db == "oracle":
            return stmt, lambda r: {n: r[idx[n]] for n in names}
        return stmt, lambda r: tuple(r[idx[n]] for n in names)

    table = cfg["table"]
    setm, keym = parse_mapping(cfg.get("set")), parse_mapping(cfg.get("keys"))
    ph = (lambda i: f":{i}") if db == "oracle" else (lambda i: "%s")
    if mode == "insert":
        pairs = setm
        stmt = (f"INSERT INTO {table} ({','.join(t for t, _ in pairs)}) "
                f"VALUES ({','.join(ph(i + 1) for i in range(len(pairs)))})")
    elif mode == "update":
        pairs = setm + keym
        sets = ", ".join(f"{t}={ph(i + 1)}" for i, (t, _) in enumerate(setm))
        where = " AND ".join(f"{t}={ph(len(setm) + i + 1)}" for i, (t, _) in enumerate(keym))
        stmt = f"UPDATE {table} SET {sets} WHERE {where}"
    else:  # upsert
        keyset = {t.lower() for t, _ in keym}
        upd = [p for p in setm if p[0].lower() not in keyset]
        if not upd:
            raise ValueError("Upsert butuh minimal satu kolom SET selain kolom kunci")
        pairs = keym + upd
        ct = [t for t, _ in pairs]
        on = " AND ".join(f"t.{t}=s.{t}" for t, _ in keym)
        upd_s = ", ".join(f"t.{t}=s.{t}" for t, _ in upd)
        ins = f"INSERT ({','.join(ct)}) VALUES ({','.join('s.' + t for t in ct)})"
        if db == "oracle":
            src = ", ".join(f"{ph(i + 1)} AS {t}" for i, t in enumerate(ct))
            stmt = (f"MERGE INTO {table} t USING (SELECT {src} FROM dual) s ON ({on}) "
                    f"WHEN MATCHED THEN UPDATE SET {upd_s} WHEN NOT MATCHED THEN {ins}")
        elif db == "mysql":
            stmt = (f"INSERT INTO {table} ({','.join(ct)}) VALUES ({','.join(['%s'] * len(ct))}) "
                    f"ON DUPLICATE KEY UPDATE {', '.join(f'{t}=VALUES({t})' for t, _ in upd)}")
        else:
            stmt = (f"MERGE {table} AS t USING (VALUES ({','.join(['%s'] * len(ct))})) AS s({','.join(ct)}) "
                    f"ON {on} WHEN MATCHED THEN UPDATE SET {upd_s} WHEN NOT MATCHED THEN {ins};")
    pos = [_idx(cols, d) for _, d in pairs]
    return stmt, lambda r: tuple(r[i] for i in pos)


def write_stream(conn, stmt, get, chunks, mode, db, commit_every=0):
    """Menulis bertahap dari iterator potongan baris. Mengembalikan (terpengaruh, perkiraan, baris_sumber).
    commit_every > 0: commit tiap N baris (data sangat besar; yang sudah di-commit tidak ikut rollback)."""
    cur = conn.cursor()
    total, n, since, unknown = 0, 0, 0, False
    many = db == "oracle" or mode == "insert" or (db == "mysql" and mode == "upsert")
    for chunk in chunks:
        if not chunk:
            continue
        if many:
            cur.executemany(stmt, [get(r) for r in chunk])
            rc = getattr(cur, "rowcount", -1)
            if rc is None or rc < 0:
                unknown, total = True, total + len(chunk)
            else:
                total += rc
        else:  # per baris supaya jumlah baris terpengaruh akurat
            for r in chunk:
                cur.execute(stmt, get(r))
                rc = getattr(cur, "rowcount", -1)
                if rc is None or rc < 0:
                    unknown, total = True, total + 1
                else:
                    total += rc
        n += len(chunk)
        since += len(chunk)
        if commit_every and since >= commit_every:
            conn.commit()
            since = 0
    return total, unknown, n
