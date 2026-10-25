from __future__ import annotations

from typing import Any, Dict, List, Optional
import json
import threading
from datetime import date
import traceback
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from .config import OUTPUT_DIR
from .database import SessionLocal
from .dbconnect import conn_info, run_query
from .dbconnect import open_connection
from .apiget import fetch_api
from .csvio import EXPORT_DIR, export_csv, make_import
from .pyscript import run_script
from .datasets import DRY_SAMPLE, QueryStream, open_stream, to_memory
from .dbdump import DUMP_DIR, build_plan, execute as run_dump
from .dbwrite import DEFAULT_MAX_ROWS as WRITE_MAX_ROWS, prepare_write, write_stream
from .derived import DERIVED, get_ref, run_derived
from .excel import build_standard, build_summary_order
from .mailer import get_settings, send_mail
from .models import Connection, Job, RunLog
from .staging import stage_table
from .ftpupload import execute_ftp_node
from .utils import now, render, safe_filename, split_emails

DAYS = {0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis", 4: "Jumat", 5: "Sabtu", 6: "Minggu"}
_locks: Dict[int, threading.Lock] = {}


def _lock(job_id: int) -> threading.Lock:
    return _locks.setdefault(job_id, threading.Lock())


SQL_VARS = ("tgl_awal", "tgl_akhir", "tgl_awal_compact", "tgl_akhir_compact")


def _as_date(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def date_context(mode: str, ref=None, override=None) -> dict:
    """Rentang data. mode 'yesterday': hari Senin -> Sabtu-Minggu. override=(dari, sampai): rentang manual."""
    ref = ref or now()
    today = ref.date() if hasattr(ref, "hour") else ref
    manual = bool(override and override[0] and override[1])
    if manual:
        s_, e_ = _as_date(override[0]), _as_date(override[1])
    elif mode == "yesterday":
        e_ = today - timedelta(days=1)
        s_ = today - timedelta(days=2) if today.weekday() == 0 else e_
    else:
        s_ = e_ = today
    if s_ == e_:
        hari = DAYS[e_.weekday()]
    elif (e_ - s_).days == 1 and s_.weekday() == 5:
        hari = "Sabtu dan Minggu"
    else:
        hari = f"{DAYS[s_.weekday()]} s/d {DAYS[e_.weekday()]}"
    fmt = lambda f: s_.strftime(f) if s_ == e_ else f"{s_.strftime(f)}_and_{e_.strftime(f)}"
    return {"hari": hari, "tgl": fmt("%d-%b-%y"), "tgl_long": fmt("%d-%b-%Y"), "tgl_title": today.strftime("%d-%m-%Y"),
            "tgl_awal": s_.isoformat(), "tgl_akhir": e_.isoformat(),
            "tgl_awal_compact": s_.strftime("%Y%m%d"), "tgl_akhir_compact": e_.strftime("%Y%m%d"),
            "lo": (today - s_).days, "hi": (today - e_).days, "abs": manual, "d_start": s_, "d_end": e_}


def sql_vars(text: str, ctx: dict) -> str:
    """Ganti {tgl_awal} dst di SQL/URL/body dengan tanggal (nilai dari objek tanggal, aman dari injection)."""
    return render(text or "", {k: ctx[k] for k in SQL_VARS})


def _day_expr(db_type: str, n: int) -> str:
    if db_type == "oracle":
        return f"trunc(sysdate-{n})" if n else "trunc(sysdate)"
    if db_type == "mysql":
        return f"date_sub(curdate(), interval {n} day)" if n else "curdate()"
    return f"cast(dateadd(day,-{n},getdate()) as date)" if n else "cast(getdate() as date)"


def _abs_expr(db_type: str, d) -> str:
    iso = d.isoformat()
    return f"date '{iso}'" if db_type == "oracle" else f"'{iso}'" if db_type == "mysql" else f"cast('{iso}' as date)"


def prepare_sql(sql, db_type, param_mode, ctx, source=None, source_col=None) -> str:
    sql = sql_vars(sql, ctx)
    if param_mode == "date":
        lo, hi = ctx["lo"], ctx["hi"]
        ex = (lambda n: _abs_expr(db_type, ctx["d_start"] if n == lo else ctx["d_end"])) if ctx["abs"] else (lambda n: _day_expr(db_type, n))
        rep = f"={ex(hi)}" if lo == hi else f" between {ex(lo)} and {ex(hi)} "
        return sql.replace("sqlParam", rep)
    if param_mode == "list":
        vals = []
        if source:
            cols, rows = source
            idx = next((i for i, c in enumerate(cols) if c.lower() == (source_col or "").lower()), None)
            if idx is None:
                raise ValueError(f"Kolom '{source_col}' tidak ada di query sumber")
            vals = list(dict.fromkeys(str(r[idx]) for r in rows if r[idx] is not None))
        lit = ",".join("'" + v.replace("'", "''") + "'" for v in vals) or "''"
        return sql.replace("sqlParam", lit)
    return sql


def _log(db, run, msg):
    run.log = (run.log or "") + f"[{now():%H:%M:%S}] {msg}\n"
    db.commit()


def create_run(job_id: int, trigger: str, triggered_by: str = "", send_email: bool = True, dry_run: bool = False,
               date_from=None, date_to=None) -> int:
    with SessionLocal() as db:
        if not db.get(Job, job_id):
            raise LookupError("Job tidak ditemukan")
        if _lock(job_id).locked():
            raise RuntimeError("Job ini sedang berjalan")
        run = RunLog(job_id=job_id, trigger=trigger, triggered_by=triggered_by, send_email=send_email, dry_run=dry_run,
                     date_from=date_from, date_to=date_to)
        db.add(run)
        db.commit()
        return run.id


def start_async(run_id: int):
    threading.Thread(target=execute_run, args=(run_id,), daemon=True).start()


def _cleanup(days: int):
    cutoff = now().timestamp() - days * 86400
    for f in list(OUTPUT_DIR.rglob("*.xlsx")) + list(EXPORT_DIR.rglob("*.csv")) + list(EXPORT_DIR.rglob("*.meta.json")) + list(DUMP_DIR.rglob("*.sql*")):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def execute_run(run_id: int):
    with SessionLocal() as db:
        run = db.get(RunLog, run_id)
        job = db.get(Job, run.job_id)
        lock = _lock(job.id)
        if not lock.acquire(blocking=False):
            run.status, run.error, run.finished_at = "failed", "Job sedang berjalan", now()
            db.commit()
            return
        try:
            _execute(db, run, job)
        except Exception as e:
            run.status = "failed"
            run.error = f"{type(e).__name__}: {e}"
            _log(db, run, "GAGAL\n" + traceback.format_exc())
        finally:
            run.finished_at = now()
            db.commit()
            lock.release()
        try:
            _cleanup(int(get_settings(db).get("retention_days") or 30))
        except Exception:
            pass


class Writers:
    """Koneksi tulis dibuka sekali per run; commit hanya jika seluruh alur berhasil (per koneksi)."""

    def __init__(self):
        self.conns = {}

    def get(self, row, info):
        if row.id not in self.conns:
            self.conns[row.id] = open_connection(info)
        return self.conns[row.id]

    def commit(self):
        for c in self.conns.values():
            c.commit()

    def rollback(self):
        for c in self.conns.values():
            try:
                c.rollback()
            except Exception:
                pass

    def close(self):
        for c in self.conns.values():
            try:
                c.close()
            except Exception:
                pass


def _execute(db, run, job):
    writers = Writers()
    try:
        _flow(db, run, job, writers)
    except Exception:
        writers.rollback()
        raise
    finally:
        writers.close()


def _need(results, cfg, q):
    data = get_ref(results, cfg.get("source"))
    if data is None:
        raise ValueError(f"Node '{q.name}': data sumber belum dipilih atau belum dijalankan")
    return data


def _flow(db, run, job, writers):
    ctx = date_context(job.date_mode, run.started_at, (run.date_from, run.date_to))
    dry = bool(run.dry_run)
    _log(db, run, f"Mulai job '{job.name}' ({run.trigger}{', DRY-RUN' if dry else ''}); "
                  f"rentang data {ctx['tgl_awal']} s/d {ctx['tgl_akhir']}{' (manual)' if ctx['abs'] else ''}")
    results, sheets = {}, []

    def add_sheet(q, name, data):
        if not isinstance(data, tuple):
            raise ValueError(f"Node '{q.name}': hasil stream/CSV tidak bisa dijadikan sheet")
        sheets.append((name, *data))

    for q in job.queries:
        cfg = json.loads(q.config) if q.config else {}
        if q.kind == "compare":
            raise ValueError(f"Node '{q.name}' (Bandingkan) sudah dihapus dari aplikasi; hapus node ini dari job")
        if q.kind == "py_script":
            mx = int(cfg.get("max_rows") or 500000)
            ins = []
            for ref in cfg.get("inputs") or []:
                data = get_ref(results, ref)
                if data is None:
                    raise ValueError(f"Node '{q.name}': input {ref} belum dipilih atau belum dijalankan")
                ins.append(to_memory(data, mx, f"Input skrip '{q.name}'"))
            params = {**{k: ctx[k] for k in SQL_VARS}, "hari": ctx["hari"], "tgl": ctx["tgl"], "tgl_long": ctx["tgl_long"], "dry_run": dry}
            main, extras, out = run_script(cfg, ins, params)
            if len(main[1]) > mx:
                raise ValueError(f"Node '{q.name}': hasil skrip {len(main[1])} baris melebihi batas {mx}")
            results[q.position] = main
            results[f"{q.position}:matched"] = main
            for port, data in extras.items():
                results[f"{q.position}:{port}"] = data
            _log(db, run, f"Skrip '{q.name}': {len(ins)} input -> {len(main[1])} baris" + "".join(f", {k}: {len(v[1])}" for k, v in extras.items()))
            if out.strip():
                _log(db, run, "Keluaran skrip:\n" + out.strip()[-8000:])
            if q.is_output:
                add_sheet(q, q.sheet_name, main)
                if cfg.get("sheet_extra"):
                    for port, data in extras.items():
                        add_sheet(q, f"{q.sheet_name} {port}", data)
            continue
        if q.kind == "db_dump":
            conn = db.get(Connection, q.connection_id) if q.connection_id else None
            if not conn:
                raise ValueError(f"Node '{q.name}': koneksi database belum dipilih")
            plan = build_plan(conn_info(conn), cfg, render(cfg.get("filename") or "dump_{tgl}", ctx), job.key)
            where = ", ".join(plan.tables) if plan.tables else "seluruh database/skema"
            if dry:
                _log(db, run, f"DRY-RUN: dump '{q.name}' tidak dijalankan. Perintah:\n{plan.desc}")
                row = [plan.out_path.name, None, 0, where]
            else:
                _log(db, run, f"Menjalankan dump '{q.name}' @ {conn.name} ({where}):\n{plan.desc}")
                res = run_dump(plan, int(cfg.get("timeout") or 3600))
                _log(db, run, f"Dump selesai: {res['file']}" + (f" ({res['size_mb']} MB)" if res["size_mb"] is not None else "") + f", {res['seconds']} dtk")
                row = [res["file"], res["size_mb"], res["seconds"], where]
            results[q.position] = (["FILE", "UKURAN_MB", "DURASI_DETIK", "TABEL"], [row])
            if q.is_output:
                add_sheet(q, q.sheet_name, results[q.position])
            continue
        if q.kind in DERIVED:
            main, extras = run_derived(q.kind, cfg, results)
            results[q.position] = main
            results[f"{q.position}:matched"] = main
            for port, data in extras.items():
                results[f"{q.position}:{port}"] = data
            more = "".join(f", {k}: {len(v[1])}" for k, v in extras.items())
            _log(db, run, f"Node '{q.name}' ({q.kind}): {len(main[1])} baris{more}")
            if q.is_output:
                add_sheet(q, q.sheet_name, main)
                if cfg.get("sheet_extra"):
                    for port, label in (("left_only", "hanya kiri"), ("right_only", "hanya kanan")):
                        if port in extras:
                            add_sheet(q, f"{q.sheet_name} {label}", extras[port])
            continue
        if q.kind == "api_get":
            cols, rows = fetch_api(cfg, lambda t: sql_vars(t, ctx))
            results[q.position] = (cols, rows)
            _log(db, run, f"API '{q.name}': {len(rows)} baris, {len(cols)} kolom")
            if q.is_output:
                add_sheet(q, q.sheet_name, results[q.position])
            continue
        if q.kind == "csv_export":
            ds = export_csv(_need(results, cfg, q), job.key, cfg, dry)
            results[q.position] = ds
            _log(db, run, f"Export CSV '{q.name}': {ds.rows_total} baris -> {ds.path.relative_to(EXPORT_DIR.parent).as_posix()} "
                          f"({ds.path.stat().st_size / 1048576:.1f} MB){' [DRY-RUN: sampel ' + str(DRY_SAMPLE) + ' baris]' if dry else ''}")
            continue
        if q.kind == "csv_import":
            ds = make_import(cfg, results, get_ref)
            results[q.position] = ds
            _log(db, run, f"Import CSV '{q.name}': {ds.path.name}" + (f" ({ds.rows_total} baris)" if ds.rows_total is not None else ""))
            continue
        if q.kind == "db_write":
            data = _need(results, cfg, q)
            target = db.get(Connection, cfg.get("target_connection_id") or 0)
            if not target:
                raise ValueError(f"Node '{q.name}': database tujuan belum dipilih")
            if not target.allow_write:
                raise ValueError(f"Koneksi '{target.name}' belum diizinkan menulis (centang di menu Koneksi database)")
            info = conn_info(target)
            sql_text = sql_vars(q.sql, ctx)
            wconn = writers.get(target, info)
            if isinstance(data, tuple):
                cols, rows = data
                mx = int(cfg.get("max_rows") or WRITE_MAX_ROWS)
                if len(rows) > mx:
                    raise ValueError(f"Node '{q.name}': {len(rows)} baris melebihi batas tulis {mx}")
                stmt, get = prepare_write(info.db_type, cfg, sql_text, cols)
                affected, approx, n = write_stream(wconn, stmt, get, [rows[i:i + 500] for i in range(0, len(rows), 500)],
                                                   cfg["mode"], info.db_type)
                note = ""
            else:  # stream / CSV: ukuran tidak dibatasi; dry-run hanya sampel
                with open_stream(data, DRY_SAMPLE if dry else None) as st:
                    cols = st.cols
                    stmt, get = prepare_write(info.db_type, cfg, sql_text, cols)
                    affected, approx, n = write_stream(wconn, stmt, get, st.chunks(), cfg["mode"], info.db_type,
                                                       0 if dry else int(cfg.get("commit_every") or 0))
                note = f" [stream{', SAMPEL ' + str(DRY_SAMPLE) + ' baris pertama' if dry else ''}]"
            _log(db, run, f"Tulis '{q.name}' ({cfg['mode']}) -> {target.name}: {n} baris sumber, "
                          f"{affected} baris terpengaruh{' (perkiraan)' if approx else ''}{note}"
                          f"{' [DRY-RUN: akan di-rollback]' if dry else ''}")
            results[q.position] = (["NODE", "MODE", "BARIS_SUMBER", "BARIS_TERPENGARUH", "DRY_RUN"],
                                   [[q.name, cfg["mode"], n, affected, "ya" if dry else "tidak"]])
            if q.is_output:
                add_sheet(q, q.sheet_name, results[q.position])
            continue

        conn = db.get(Connection, q.connection_id) if q.connection_id else None
        if not conn:
            raise ValueError(f"Query '{q.name}': koneksi belum dipilih")
        info = conn_info(conn)
        source = None
        if q.param_mode == "list" and q.param_source_position in results:
            source = to_memory(results[q.param_source_position], 100000, f"Parameter '{q.name}'")
        sql = prepare_sql(q.sql, info.db_type, q.param_mode, ctx, source, q.param_source_column)
        if q.kind == "sql" and cfg.get("stream"):
            results[q.position] = QueryStream(info, sql)
            _log(db, run, f"Query '{q.name}' @ {conn.name}: mode stream (dijalankan saat dipakai node berikutnya)")
            if q.is_output:
                add_sheet(q, q.sheet_name, results[q.position])
            continue
        cols, rows = run_query(info, sql)
        results[q.position] = (cols, rows)
        _log(db, run, f"Query '{q.name}' @ {conn.name}: {len(rows)} baris")
        if q.kind == "stage":
            target = db.get(Connection, cfg.get("target_connection_id") or 0)
            if not target:
                raise ValueError(f"Langkah '{q.name}': database tujuan staging belum dipilih")
            table, warns = stage_table(conn_info(target), cfg.get("table"), cols, rows)
            _log(db, run, f"Staging: {len(rows)} baris -> {target.name}.{table}")
            for w in warns:
                _log(db, run, f"Peringatan: {w}")
        if q.is_output:
            add_sheet(q, q.sheet_name, results[q.position])

    path = None
    if job.layout == "summary_order" or sheets:
        fname = safe_filename(render(job.filename_tpl, ctx)) + ".xlsx"
        folder = OUTPUT_DIR / job.key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / fname
        if job.layout == "summary_order":
            build_summary_order(path, results, ctx["tgl_title"])
        else:
            build_standard(path, sheets)
        run.file_name, run.file_path = fname, f"{job.key}/{fname}"
        _log(db, run, f"File dibuat: {fname}")
    else:
        _log(db, run, "Tidak ada node yang dijadikan sheet: file dan email dilewati")

    if writers.conns:  # commit baru setelah seluruh alur & file berhasil
        if dry:
            writers.rollback()
            _log(db, run, "DRY-RUN: perubahan database dibatalkan (rollback)")
        else:
            writers.commit()
            _log(db, run, "Perubahan database di-commit")

    if path and job.email_enabled and run.send_email and not dry:
        try:
            _send(db, job, ctx, path)
            run.email_status = "sent"
            _log(db, run, "Email terkirim")
        except Exception as e:
            run.email_status = "failed"
            run.error = f"Email gagal: {e}"
            run.status = "failed"
            _log(db, run, f"Email GAGAL: {e}")
            return
    else:
        run.email_status = "skipped"
        _log(db, run, "Email dilewati")
    run.status = "success"
    _log(db, run, "Selesai")


def _send(db, job, ctx, path: Path):
    send_mail(get_settings(db), split_emails(job.email_to), split_emails(job.email_cc),
              render(job.subject, ctx), render(job.body_html, ctx), path)


def resend_email(run_id: int):
    """Kirim ulang email dari file hasil run sebelumnya (jika excel sudah jadi tapi email gagal)."""
    with SessionLocal() as db:
        run = db.get(RunLog, run_id)
        job = db.get(Job, run.job_id)
        path = OUTPUT_DIR / run.file_path if run.file_path else None
        if not path or not path.exists():
            raise FileNotFoundError("File hasil run tidak ditemukan; jalankan ulang job")
        try:
            _send(db, job, date_context(job.date_mode, run.started_at, (run.date_from, run.date_to)), path)
        except Exception as e:
            _log(db, run, f"Kirim ulang email GAGAL: {e}")
            raise
        run.email_status, run.status, run.error = "sent", "success", ""
        _log(db, run, "Email terkirim (kirim ulang manual)")


def recover_stale_runs():
    with SessionLocal() as db:
        for r in db.scalars(select(RunLog).where(RunLog.status == "running")):
            r.status, r.error, r.finished_at = "failed", "Terputus (aplikasi restart)", now()
        db.commit()
