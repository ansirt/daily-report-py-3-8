from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_URL

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    with engine.begin() as c:  # migrasi ringan untuk database yang sudah ada
        have = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(job_queries)")}
        if "kind" not in have:
            c.exec_driver_sql("ALTER TABLE job_queries ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'sql'")
        if "config" not in have:
            c.exec_driver_sql("ALTER TABLE job_queries ADD COLUMN config TEXT NOT NULL DEFAULT ''")
        have = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(connections)")}
        if "allow_write" not in have:
            c.exec_driver_sql("ALTER TABLE connections ADD COLUMN allow_write BOOLEAN NOT NULL DEFAULT 0")
        have = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(run_logs)")}
        if "dry_run" not in have:
            c.exec_driver_sql("ALTER TABLE run_logs ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT 0")
        if "date_from" not in have:
            c.exec_driver_sql("ALTER TABLE run_logs ADD COLUMN date_from VARCHAR(10)")
            c.exec_driver_sql("ALTER TABLE run_logs ADD COLUMN date_to VARCHAR(10)")
        import json  # langkah 'merge' lama -> node 'join'
        for id_, cfg in c.exec_driver_sql("SELECT id, config FROM job_queries WHERE kind='merge'").fetchall():
            d = json.loads(cfg or "{}")
            d["join_type"] = d.pop("how", d.get("join_type", "left"))
            d.setdefault("output", "single")
            c.exec_driver_sql("UPDATE job_queries SET kind='join', config=? WHERE id=?", (json.dumps(d), id_))
