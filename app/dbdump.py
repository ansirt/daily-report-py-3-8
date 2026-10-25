from __future__ import annotations

"""Node Dump database: menjalankan CLI dump bawaan (MySQL: mysqldump, Oracle: expdp) dengan opsi dari katalog.
Argumen disusun dari katalog (whitelist), tanpa shell, dan password tidak ditaruh di argumen perintah."""
import gzip
import os
import signal
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CSV_DIR

DUMP_DIR = CSV_DIR / "dumps"
_TABLE = re.compile(r"^[A-Za-z0-9_$#.\-]+$")
_SIZE = re.compile(r"^\d+[KMG]?$")
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_INT = re.compile(r"^\d{1,3}$")


def _o(key, label, flag, type="flag", default=None, choices=None, pattern=None, required=False, hint=""):
    return {"key": key, "label": label, "flag": flag, "type": type, "default": default, "choices": choices,
            "pattern": pattern, "required": required, "hint": hint}


MYSQL = [
    _o("single_transaction", "Snapshot konsisten tanpa lock (InnoDB)", "--single-transaction", default=True),
    _o("quick", "Baca baris satu per satu (hemat memori)", "--quick", default=True),
    _o("no_tablespaces", "Lewati tablespace (hindari error hak PROCESS)", "--no-tablespaces"),
    _o("column_statistics_off", "Nonaktifkan column-statistics (klien MySQL 8 ke server lama)", "--column-statistics=0"),
    _o("routines", "Sertakan stored procedure dan function", "--routines"),
    _o("events", "Sertakan event scheduler", "--events"),
    _o("skip_triggers", "Lewati trigger", "--skip-triggers"),
    _o("no_data", "Struktur saja (tanpa data)", "--no-data"),
    _o("no_create_info", "Data saja (tanpa CREATE TABLE)", "--no-create-info"),
    _o("skip_add_drop_table", "Jangan tulis DROP TABLE sebelum CREATE", "--skip-add-drop-table"),
    _o("complete_insert", "INSERT dengan nama kolom lengkap", "--complete-insert"),
    _o("skip_extended_insert", "Satu INSERT per baris (file lebih besar, mudah dibaca)", "--skip-extended-insert"),
    _o("insert_ignore", "INSERT IGNORE", "--insert-ignore"),
    _o("replace", "REPLACE INTO, bukan INSERT", "--replace"),
    _o("hex_blob", "Kolom biner ditulis heksadesimal", "--hex-blob"),
    _o("skip_lock_tables", "Jangan LOCK TABLES", "--skip-lock-tables"),
    _o("order_by_primary", "Urutkan baris menurut primary key", "--order-by-primary"),
    _o("skip_comments", "Tanpa komentar di file", "--skip-comments"),
    _o("compress_protocol", "Kompres lalu lintas klien-server", "--compress"),
    _o("databases", "Sertakan CREATE DATABASE (seluruh database; pilihan tabel diabaikan)", "--databases"),
    _o("where", "Filter baris (WHERE)", "--where=", "text", hint="mis. tgl >= '2026-01-01'; berlaku ke semua tabel terpilih"),
    _o("default_character_set", "Character set", "--default-character-set=", "choice", choices=["utf8mb4", "utf8", "latin1"]),
    _o("set_gtid_purged", "set-gtid-purged", "--set-gtid-purged=", "choice", choices=["OFF", "ON", "AUTO"]),
    _o("ssl_mode", "ssl-mode", "--ssl-mode=", "choice", choices=["DISABLED", "PREFERRED", "REQUIRED"]),
    _o("max_allowed_packet", "max-allowed-packet", "--max-allowed-packet=", "value", pattern=_SIZE, hint="mis. 512M"),
    _o("net_buffer_length", "net-buffer-length", "--net-buffer-length=", "value", pattern=_SIZE, hint="mis. 16K"),
]
ORACLE = [
    _o("directory", "Directory object di server Oracle", "directory=", "value", default="DATA_PUMP_DIR", pattern=_IDENT, required=True,
       hint="File .dmp ditulis di server database (folder milik directory object), bukan di server aplikasi"),
    _o("content", "Isi", "content=", "choice", default="ALL", choices=["ALL", "DATA_ONLY", "METADATA_ONLY"]),
    _o("compression", "Kompresi", "compression=", "choice", choices=["NONE", "METADATA_ONLY", "DATA_ONLY", "ALL"],
       hint="ALL / DATA_ONLY butuh lisensi Advanced Compression"),
    _o("parallel", "Parallel worker", "parallel=", "value", pattern=_INT),
    _o("flashback", "Snapshot konsisten (flashback_time=systimestamp)", "flashback_time=systimestamp", default=False),
    _o("exclude_statistics", "Tanpa statistik", "exclude=STATISTICS"),
    _o("exclude_index", "Tanpa index", "exclude=INDEX"),
    _o("exclude_grant", "Tanpa grant", "exclude=GRANT"),
    _o("reuse_dumpfiles", "Timpa file dump bernama sama", "reuse_dumpfiles=YES"),
    _o("query", "Filter baris (QUERY)", "query=", "text", hint='format Oracle: TABEL:"WHERE tgl >= DATE \'2026-01-01\'"'),
    _o("schema_dump", "Dump seluruh skema akun login (jika tabel tidak dipilih)", "", default=False),
]
TOOLS = {"mysql": ("mysqldump", MYSQL, "MYSQLDUMP_PATH"), "oracle": ("expdp", ORACLE, "EXPDP_PATH")}


def catalog():
    return {t: {"tool": v[0], "options": v[1]} for t, v in TOOLS.items()}


def features():
    return {"mysqldump": bool(_binary("mysql")), "expdp": bool(_binary("oracle"))}


def kill_tree(proc):
    """Hentikan proses beserta turunannya (POSIX: process group; Windows: proses langsung)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _popen(argv, **kw):
    return subprocess.Popen(argv, start_new_session=hasattr(os, "killpg"), **kw)


def _binary(db_type):
    tool, _, env = TOOLS[db_type]
    return os.getenv(env) or shutil.which(tool)


def _tables(cfg):
    t = cfg.get("tables") or []
    items = re.split(r"[\n,;]+", t) if isinstance(t, str) else t
    out = [x.strip() for x in items if x and x.strip()]
    for x in out:
        if not _TABLE.match(x):
            raise ValueError(f"Nama tabel tidak valid: '{x}'")
    return list(dict.fromkeys(out))


def _checked_options(db_type, cfg):
    """[(opsi, nilai)] yang aktif, sudah divalidasi terhadap katalog."""
    if db_type not in TOOLS:
        raise ValueError("Dump hanya tersedia untuk MySQL (mysqldump) dan Oracle (expdp). Untuk SQL Server pakai node Export CSV.")
    given, out = cfg.get("options") or {}, []
    for o in TOOLS[db_type][1]:
        v = given.get(o["key"], o["default"])
        if o["required"] and v in (None, ""):
            raise ValueError(f"Opsi '{o['label']}' wajib diisi")
        if o["type"] == "flag":
            if v:
                out.append((o, True))
            continue
        if v in (None, ""):
            continue
        v = str(v)
        if "\n" in v or "\r" in v or "\x00" in v:
            raise ValueError(f"Nilai opsi '{o['label']}' tidak valid")
        if o["type"] == "choice" and v not in o["choices"]:
            raise ValueError(f"Nilai '{v}' tidak valid untuk '{o['label']}'")
        if o["type"] == "value" and not o["pattern"].match(v):
            raise ValueError(f"Nilai '{v}' tidak valid untuk '{o['label']}'")
        out.append((o, v))
    return out


def validate_dump(cfg, db_type):
    opts = _checked_options(db_type, cfg)
    tables, keys = _tables(cfg), {o["key"] for o, _ in opts}
    if db_type == "mysql" and "databases" in keys and tables:
        raise ValueError("Opsi 'databases' tidak bisa dipadukan dengan pilihan tabel")
    if db_type == "oracle" and not tables and "schema_dump" not in keys:
        raise ValueError("Pilih tabel, atau aktifkan 'dump seluruh skema akun login'")
    if not (cfg.get("filename") or "").strip():
        raise ValueError("Nama file dump kosong")


@dataclass
class Plan:
    db_type: str
    argv: list
    env: dict = field(default_factory=dict)
    parfile: str = ""
    out_path: Path = None
    desc: str = ""
    tables: list = field(default_factory=list)
    compress: bool = False


def build_plan(info, cfg, name, job_key):
    validate_dump(cfg, info.db_type)
    binary = _binary(info.db_type)
    tool = TOOLS[info.db_type][0]
    tables, opts = _tables(cfg), _checked_options(info.db_type, cfg)
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name).strip("._") or "dump"
    exe = binary or tool
    if info.db_type == "mysql":
        argv = [exe, f"--host={info.host}", f"--port={info.port}", f"--user={info.username}"]
        keys = set()
        for o, v in opts:
            keys.add(o["key"])
            argv.append(o["flag"] if o["type"] == "flag" else o["flag"] + v)
        argv += ["--databases", info.database] if "databases" in keys else [info.database, *tables]
        gz = cfg.get("compress", True) is not False
        out = DUMP_DIR / job_key / (safe + (".sql.gz" if gz else ".sql"))
        desc = "MYSQL_PWD=**** " + shlex.join(argv) + f" > {out.name}"
        return Plan("mysql", argv, {"MYSQL_PWD": info.password}, "", out, desc, tables, gz)
    if re.search(r'["\s]', info.password or ""):
        raise ValueError("Password mengandung spasi atau tanda kutip: tidak didukung untuk parfile expdp")
    target = info.dsn or f"//{info.host}:{info.port}/{info.database}"
    lines = [f"userid={info.username}/{info.password}@{target}", f"dumpfile={safe}.dmp", f"logfile={safe}.log"]
    keys = set()
    for o, v in opts:
        keys.add(o["key"])
        if o["key"] != "schema_dump":
            lines.append(o["flag"] if o["type"] == "flag" else o["flag"] + v)
    if tables:
        lines.append("tables=" + ",".join(tables))
    else:
        lines.append(f"schemas={info.username.upper()}")
    masked = "\n".join(f"userid={info.username}/****@{target}" if l.startswith("userid=") else l for l in lines)
    return Plan("oracle", [exe, "parfile=<sementara>"], {}, "\n".join(lines) + "\n", DUMP_DIR / job_key / (safe + ".dmp"),
                f"{tool} parfile=<sementara> dengan isi:\n{masked}", tables)


def execute(plan, timeout):
    """Menjalankan dump. Mengembalikan {file, size_mb, seconds}."""
    if not plan.argv[0] or not (Path(plan.argv[0]).exists() or shutil.which(plan.argv[0])):
        tool, _, env = TOOLS[plan.db_type]
        raise ValueError(f"{tool} tidak ditemukan. Pasang klien database di server aplikasi atau setel {env}")
    t0 = time.time()
    if plan.db_type == "oracle":
        with tempfile.TemporaryDirectory(prefix="expdp_") as tmp:
            par = Path(tmp) / "par.txt"
            fd = os.open(par, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(plan.parfile)
            proc = _popen([plan.argv[0], f"parfile={par}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                so, se = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.communicate()
                raise ValueError(f"expdp melebihi batas waktu {timeout} detik dan dihentikan")
        if proc.returncode != 0:
            tail = (so + se).decode("utf-8", "replace")[-2000:]
            raise ValueError(f"expdp gagal (kode {proc.returncode}):\n{tail}")
        return {"file": plan.out_path.name + " (di server database)", "size_mb": None, "seconds": round(time.time() - t0)}
    plan.out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.out_path.with_name(plan.out_path.name + ".part")
    proc = _popen(plan.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **plan.env})
    err, killed = [], []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    drain.start()

    def kill():
        killed.append(1)
        kill_tree(proc)
    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        with (gzip.open(tmp, "wb") if plan.compress else open(tmp, "wb")) as w:
            shutil.copyfileobj(proc.stdout, w, 1 << 20)
        rc = proc.wait()
    finally:
        timer.cancel()
        drain.join(5)
    tail = (err[0] if err else b"").decode("utf-8", "replace")[-2000:]
    if killed or rc != 0:
        tmp.unlink(missing_ok=True)
        if killed:
            raise ValueError(f"mysqldump melebihi batas waktu {timeout} detik dan dihentikan")
        raise ValueError(f"mysqldump gagal (kode {rc}):\n{tail}")
    os.replace(tmp, plan.out_path)
    return {"file": plan.out_path.relative_to(CSV_DIR).as_posix(), "size_mb": round(plan.out_path.stat().st_size / 1048576, 2),
            "seconds": round(time.time() - t0)}
