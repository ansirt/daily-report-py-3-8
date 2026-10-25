from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse,FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import COOKIE_SECURE, CSV_DIR, OUTPUT_DIR, TOKEN_HOURS
from .database import get_db
from .dbconnect import conn_info, run_query, test_connection, validate_readonly
from .apiget import fetch_api, public_config, secure_config, validate_api
from .csvio import safe_csv_name, validate_csv_export, validate_csv_import
from .dbdump import DUMP_DIR, build_plan, catalog as dump_catalog, features as dump_features, validate_dump
from .dbwrite import validate_dbwrite
from .pyscript import features as py_features, validate_py
from .derived import DERIVED, validate_derived
from .mailer import get_settings, send_mail, set_setting
from .models import Connection, Job, JobQuery, RunLog, User
from .runner import create_run, date_context, prepare_sql, resend_email, sql_vars, start_async
from .scheduler import next_run, sync_jobs, validate_cron
from .staging import check_table
from .security import create_token, encrypt, hash_password, require_user, verify_password
from .utils import iso, jsonable, render, split_emails

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

# ---------------- schemas ----------------
class LoginIn(BaseModel):
    username: str
    password: str

class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)

class ConnIn(BaseModel):
    name: str = Field(min_length=1)
    db_type: Literal["oracle", "mysql", "mssql", "ftp", "sftp"]
    host: str = ""
    port: Optional[int] = None
    database: str = ""
    username: str = ""
    password: Optional[str] = None
    dsn: str = ""
    note: str = ""
    allow_write: bool = False

class QueryIn(BaseModel):
    id: Optional[int] = None
    name: str = ""
    sheet_name: str = "Sheet1"
    connection_id: Optional[int] = None
    sql: str = ""
    kind: Literal["sql", "stage", "join", "merge", "compare", "pivot", "db_write", "api_get", "csv_export", "csv_import",
        "py_script",
        "db_dump",
        "ftp_upload",] = "sql"
    config: dict = {}
    is_output: bool = True
    param_mode: Literal["none", "date", "list"] = "none"
    param_source_position: Optional[int] = None
    param_source_column: Optional[str] = None

class JobIn(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    enabled: bool = False
    cron: str
    date_mode: Literal["today", "yesterday"] = "today"
    layout: Literal["standard", "summary_order"] = "standard"
    email_enabled: bool = True
    email_to: str = ""
    email_cc: str = ""
    subject: str = ""
    body_html: str = ""
    filename_tpl: str = "report_{tgl}"
    queries: List[QueryIn] = []

class RunIn(BaseModel):
    send_email: bool = True
    dry_run: bool = False
    date_from: Optional[date] = None  # rentang manual (opsional); isi keduanya
    date_to: Optional[date] = None

class SmtpIn(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    smtp_auth: Literal["login", "ntlm"] = "login"
    smtp_sender: str = ""
    smtp_user: str = ""
    smtp_password: Optional[str] = None
    retention_days: int = Field(30, ge=1, le=3650)

class TestMailIn(BaseModel):
    to: str

class ApiPreviewIn(BaseModel):
    config: dict
    query_id: Optional[int] = None
    date_mode: Literal["today", "yesterday"] = "today"

class PreviewIn(BaseModel):
    connection_id: int
    sql: str
    param_mode: Literal["none", "date", "list"] = "none"
    date_mode: Literal["today", "yesterday"] = "today"

# ---------------- auth ----------------
_fails: Dict[str, List[float]] = {}

@public.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else '-'}:{body.username.lower()}"
    recent = [t for t in _fails.get(key, []) if time.time() - t < 300]
    _fails[key] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Terlalu banyak percobaan, coba lagi 5 menit lagi")
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        _fails[key].append(time.time())
        raise HTTPException(401, "Username atau password salah")
    _fails.pop(key, None)
    response.set_cookie("session", create_token(user.username), httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, max_age=TOKEN_HOURS * 3600)
    return {"username": user.username}

@public.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}

@api.get("/auth/me")
def me(user: str = Depends(require_user)):
    return {"username": user}

@api.post("/auth/change-password")
def change_password(body: PasswordIn, user: str = Depends(require_user), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == user))
    if not verify_password(body.old_password, u.password_hash):
        raise HTTPException(400, "Password lama salah")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}

# ---------------- connections ----------------
def conn_out(c: Connection):
    return {"id": c.id, "name": c.name, "db_type": c.db_type, "host": c.host, "port": c.port,
            "database": c.database, "username": c.username, "dsn": c.dsn, "note": c.note, "allow_write": c.allow_write,
            "has_password": bool(c.password_enc)}

@api.get("/connections")
def list_connections(db: Session = Depends(get_db)):
    return [conn_out(c) for c in db.scalars(select(Connection).order_by(Connection.name))]

def _save_conn(c: Connection, b: ConnIn, db: Session):
    dup = db.scalar(select(Connection).where(Connection.name == b.name, Connection.id != (c.id or 0)))
    if dup:
        raise HTTPException(409, "Nama koneksi sudah dipakai")
    for f in ("name", "db_type", "host", "port", "database", "username", "dsn", "note", "allow_write"):
        setattr(c, f, getattr(b, f))
    if b.password:
        c.password_enc = encrypt(b.password)
    db.add(c)
    db.commit()
    return conn_out(c)

@api.post("/connections")
def create_connection(b: ConnIn, db: Session = Depends(get_db)):
    return _save_conn(Connection(), b, db)

@api.put("/connections/{cid}")
def update_connection(cid: int, b: ConnIn, db: Session = Depends(get_db)):
    c = db.get(Connection, cid) or _404()
    return _save_conn(c, b, db)

@api.delete("/connections/{cid}")
def delete_connection(cid: int, db: Session = Depends(get_db)):
    c = db.get(Connection, cid) or _404()
    used = db.scalar(select(func.count()).select_from(JobQuery).where(JobQuery.connection_id == cid))
    if used:
        raise HTTPException(409, f"Koneksi dipakai oleh {used} query")
    db.delete(c)
    db.commit()
    return {"ok": True}

@api.post("/connections/{cid}/test")
def test_conn(cid: int, db: Session = Depends(get_db)):
    c = db.get(Connection, cid) or _404()
    try:
        test_connection(conn_info(c))
        return {"ok": True, "message": "Koneksi berhasil"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}

def _404():
    raise HTTPException(404, "Data tidak ditemukan")

# ---------------- jobs ----------------
def run_out(r: RunLog, detail=False):
    d = {"id": r.id, "job_id": r.job_id, "job_name": r.job.name if r.job else "", "trigger": r.trigger,
         "triggered_by": r.triggered_by, "dry_run": r.dry_run, "date_from": r.date_from, "date_to": r.date_to, "status": r.status, "started_at": iso(r.started_at),
         "finished_at": iso(r.finished_at), "file_name": r.file_name, "email_status": r.email_status,
         "error": r.error, "has_file": bool(r.file_path),
         "duration": int((r.finished_at - r.started_at).total_seconds()) if r.finished_at else None}
    if detail:
        d["log"] = r.log
    return d

def _public(q):
    cfg = json.loads(q.config) if q.config else {}
    return public_config(cfg) if q.kind == "api_get" else cfg

def job_out(j: Job, db: Session, detail=False):
    last = db.scalars(select(RunLog).where(RunLog.job_id == j.id).order_by(RunLog.id.desc()).limit(1)).first()
    d = {"id": j.id, "key": j.key, "name": j.name, "description": j.description, "enabled": j.enabled,
         "cron": j.cron, "date_mode": j.date_mode, "layout": j.layout, "email_enabled": j.email_enabled,
         "email_to": j.email_to, "email_cc": j.email_cc, "subject": j.subject, "body_html": j.body_html,
         "filename_tpl": j.filename_tpl, "writes": any(q.kind == "db_write" for q in j.queries), "next_run": next_run(j.id), "last_run": run_out(last) if last else None}
    if detail:
        d["queries"] = [{"id": q.id, "name": q.name, "sheet_name": q.sheet_name, "connection_id": q.connection_id,
                         "sql": q.sql, "is_output": q.is_output, "param_mode": q.param_mode,
                         "param_source_position": q.param_source_position,
                         "param_source_column": q.param_source_column, "kind": q.kind,
                         "config": _public(q)} for q in j.queries]
    return d

def _apply_job(job: Job, b: JobIn, db: Session):
    db.autoflush = False  # job baru belum lengkap saat validasi melakukan query
    try:
        validate_cron(b.cron)
    except ValueError as e:
        raise HTTPException(422, f"Jadwal cron tidak valid: {e}")
    for q in b.queries:
        try:
            if q.kind in ("sql", "stage"):
                validate_readonly(q.sql)
            if q.kind == "stage":
                if not q.config.get("target_connection_id"):
                    raise ValueError("Database tujuan staging belum dipilih")
                q.config["table"] = check_table(q.config.get("table"))
            if q.kind in DERIVED:
                validate_derived(q.kind, q.config)
            if q.kind == "db_write":
                validate_dbwrite(q.config, q.sql)
            if q.kind == "compare":
                raise ValueError("Node Bandingkan sudah dihapus; hapus node ini dari job")
            if q.kind == "py_script":
                validate_py(q.config)
            if q.kind == "db_dump":
                c = db.get(Connection, q.connection_id or 0)
                if not c:
                    raise ValueError("Koneksi database belum dipilih")
                validate_dump(q.config, c.db_type)
            if q.kind == "api_get":
                validate_api(q.config)
            if q.kind == "csv_export":
                validate_csv_export(q.config)
            if q.kind == "csv_import":
                validate_csv_import(q.config)
        except ValueError as e:
            raise HTTPException(422, f"Langkah '{q.name}': {e}")
    for f in ("name", "description", "enabled", "cron", "date_mode", "layout", "email_enabled",
              "email_to", "email_cc", "subject", "body_html", "filename_tpl"):
        setattr(job, f, getattr(b, f))
    existing = {q.id: q for q in job.queries}
    new = []
    for i, qd in enumerate(b.queries):
        q = existing.get(qd.id) or JobQuery()
        for f in ("name", "sheet_name", "connection_id", "sql", "is_output", "param_mode",
                  "param_source_position", "param_source_column"):
            setattr(q, f, getattr(qd, f))
        cfg = qd.config
        if qd.kind == "api_get":
            cfg = secure_config(cfg, json.loads(q.config) if q.config else {})
        q.kind, q.config = qd.kind, json.dumps(cfg)
        q.sheet_name = q.sheet_name[:31]
        q.position = i
        new.append(q)
    job.queries = new

@api.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [job_out(j, db) for j in db.scalars(select(Job).order_by(Job.id))]

@api.get("/jobs/{jid}")
def get_job(jid: int, db: Session = Depends(get_db)):
    return job_out(db.get(Job, jid) or _404(), db, detail=True)

@api.post("/jobs")
def create_job(b: JobIn, db: Session = Depends(get_db)):
    base = re.sub(r"[^a-z0-9]+", "_", b.name.lower()).strip("_") or "job"
    key, n = base, 2
    while db.scalar(select(Job).where(Job.key == key)):
        key, n = f"{base}_{n}", n + 1
    job = Job(key=key)
    db.add(job)
    _apply_job(job, b, db)
    db.commit()
    sync_jobs()
    return job_out(job, db, detail=True)

@api.put("/jobs/{jid}")
def update_job(jid: int, b: JobIn, db: Session = Depends(get_db)):
    job = db.get(Job, jid) or _404()
    _apply_job(job, b, db)
    db.commit()
    sync_jobs()
    return job_out(job, db, detail=True)

@api.delete("/jobs/{jid}")
def delete_job(jid: int, db: Session = Depends(get_db)):
    db.delete(db.get(Job, jid) or _404())
    db.commit()
    sync_jobs()
    return {"ok": True}

@api.post("/jobs/{jid}/run")
def run_job(jid: int, b: RunIn, user: str = Depends(require_user)):
    if bool(b.date_from) != bool(b.date_to):
        raise HTTPException(422, "Isi tanggal awal dan tanggal akhir sekaligus")
    if b.date_from and (b.date_from > b.date_to or (b.date_to - b.date_from).days > 400):
        raise HTTPException(422, "Rentang tanggal tidak valid (awal <= akhir, maksimal 400 hari)")
    try:
        run_id = create_run(jid, "manual", user, b.send_email, b.dry_run,
                            b.date_from.isoformat() if b.date_from else None, b.date_to.isoformat() if b.date_to else None)
    except LookupError:
        _404()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    start_async(run_id)
    return {"run_id": run_id}

@api.post("/queries/preview")
def preview(b: PreviewIn, db: Session = Depends(get_db)):
    c = db.get(Connection, b.connection_id) or _404()
    info = conn_info(c)
    try:
        sql = prepare_sql(b.sql, info.db_type, b.param_mode, date_context(b.date_mode))
        cols, rows = run_query(info, sql, limit=20)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows]}

@api.post("/api-preview")
def api_preview(b: ApiPreviewIn, db: Session = Depends(get_db)):
    old = {}
    if b.query_id:
        q = db.get(JobQuery, b.query_id)
        old = json.loads(q.config) if q and q.config else {}
    try:
        cfg = secure_config(b.config, old)
        validate_api(cfg)
        ctx = date_context(b.date_mode)
        cols, rows = fetch_api(cfg, lambda t: sql_vars(t, ctx), preview=True)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows[:20]]}

# ---------------- file CSV ----------------
@api.get("/csv")
def list_csv():
    files = []
    for p in CSV_DIR.rglob("*.csv"):
        st = p.stat()
        files.append({"path": p.relative_to(CSV_DIR).as_posix(), "size": st.st_size, "modified": st.st_mtime})
    files.sort(key=lambda f: -f["modified"])
    return files[:500]

@api.put("/csv/{name}")
async def upload_csv(name: str, request: Request):
    try:
        n = safe_csv_name(name)
    except ValueError as e:
        raise HTTPException(422, str(e))
    path, tmp = CSV_DIR / n, CSV_DIR / (n + ".part")
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"path": n, "size": path.stat().st_size}

# ---------------- fitur server, dump, file ----------------
@api.get("/features")
def features():
    return {**py_features(), **dump_features()}

@api.get("/dump/catalog")
def get_dump_catalog():
    return dump_catalog()

@api.get("/connections/{cid}/tables")
def connection_tables(cid: int, db: Session = Depends(get_db)):
    c = db.get(Connection, cid) or _404()
    sql = {"oracle": "select table_name from user_tables order by 1",
           "mysql": "select table_name from information_schema.tables where table_schema = database() and table_type = 'BASE TABLE' order by 1",
           "mssql": "select table_schema + '.' + table_name from information_schema.tables where table_type = 'BASE TABLE' order by 1"}[c.db_type]
    try:
        _, rows = run_query(conn_info(c), sql)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return [str(r[0]) for r in rows[:5000]]

class DumpPreviewIn(BaseModel):
    connection_id: int
    config: dict
    job_key: str = "job"

@api.post("/dump/preview")
def dump_preview(b: DumpPreviewIn, db: Session = Depends(get_db)):
    c = db.get(Connection, b.connection_id) or _404()
    try:
        plan = build_plan(conn_info(c), b.config, render(b.config.get("filename") or "dump", date_context("today")), b.job_key)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"command": plan.desc}

_FILE_EXT = (".csv", ".sql", ".gz", ".dmp")

def _safe_file(path: str):
    p = (CSV_DIR / path).resolve()
    if CSV_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "File tidak ditemukan")
    return p

@api.get("/files")
def list_files():
    out = []
    for p in CSV_DIR.rglob("*"):
        if p.is_file() and p.name.lower().endswith(_FILE_EXT):
            st = p.stat()
            out.append({"path": p.relative_to(CSV_DIR).as_posix(), "size": st.st_size, "modified": st.st_mtime})
    return sorted(out, key=lambda f: -f["modified"])[:1000]

@api.get("/files/download")
def download_file(path: str):
    p = _safe_file(path)
    return FileResponse(p, filename=p.name)

@api.delete("/files")
def delete_file(path: str):
    _safe_file(path).unlink()
    return {"ok": True}

# ---------------- runs ----------------
@api.get("/runs")
def list_runs(job_id: Optional[int] = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(RunLog).order_by(RunLog.id.desc()).limit(min(limit, 500))
    if job_id:
        q = q.where(RunLog.job_id == job_id)
    return [run_out(r) for r in db.scalars(q)]

@api.get("/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db)):
    return run_out(db.get(RunLog, rid) or _404(), detail=True)

@api.post("/runs/{rid}/resend")
def resend(rid: int, db: Session = Depends(get_db)):
    db.get(RunLog, rid) or _404()
    try:
        resend_email(rid)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"ok": True}

@api.get("/runs/{rid}/download")
def download(rid: int, db: Session = Depends(get_db)):
    r = db.get(RunLog, rid) or _404()
    path = (OUTPUT_DIR / r.file_path).resolve() if r.file_path else None
    if not path or OUTPUT_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "File sudah tidak ada")
    return FileResponse(path, filename=r.file_name)

# ---------------- email settings ----------------
@api.get("/settings")
def read_settings(db: Session = Depends(get_db)):
    s = get_settings(db)
    return {"smtp_host": s["smtp_host"], "smtp_port": int(s["smtp_port"] or 587), "smtp_security": s["smtp_security"],
            "smtp_auth": s["smtp_auth"], "smtp_sender": s["smtp_sender"], "smtp_user": s["smtp_user"],
            "smtp_password_set": bool(s["smtp_password_enc"]), "retention_days": int(s["retention_days"] or 30)}

@api.put("/settings")
def write_settings(b: SmtpIn, db: Session = Depends(get_db)):
    for k in ("smtp_host", "smtp_security", "smtp_auth", "smtp_sender", "smtp_user"):
        set_setting(db, k, getattr(b, k).strip())
    set_setting(db, "smtp_port", str(b.smtp_port))
    set_setting(db, "retention_days", str(b.retention_days))
    if b.smtp_password:
        set_setting(db, "smtp_password_enc", encrypt(b.smtp_password))
    db.commit()
    return read_settings(db)

@api.post("/settings/test-email")
def test_email(b: TestMailIn, db: Session = Depends(get_db)):
    try:
        send_mail(get_settings(db), split_emails(b.to), [], "Test email DC Report Scheduler",
                  "<p>Email uji coba dari DC Report Scheduler.</p>")
        return {"ok": True, "message": "Email uji coba terkirim"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}

@api.get("/help", response_class=HTMLResponse)
def serve_help_page():
    """Menampilkan halaman panduan penggunaan aplikasi Daily Report di tab baru."""
    help_file = Path("static/help.html")
    if help_file.exists():
        return FileResponse(help_file)
    return HTMLResponse(content="<h1>Halaman Panduan tidak ditemukan</h1>", status_code=404)
