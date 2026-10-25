from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .utils import now


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    db_type: Mapped[str] = mapped_column(String(20))  # oracle | mysql | mssql
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    database: Mapped[str] = mapped_column(String(255), default="")  # oracle: service name
    username: Mapped[str] = mapped_column(String(255), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    dsn: Mapped[str] = mapped_column(Text, default="")  # optional full Oracle descriptor / TNS alias
    note: Mapped[str] = mapped_column(String(255), default="")
    allow_write: Mapped[bool] = mapped_column(Boolean, default=False)  # izinkan node "Tulis ke database"


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cron: Mapped[str] = mapped_column(String(100), default="0 7 * * *")
    date_mode: Mapped[str] = mapped_column(String(20), default="today")  # today | yesterday
    layout: Mapped[str] = mapped_column(String(20), default="standard")  # standard | summary_order
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    email_to: Mapped[str] = mapped_column(Text, default="")
    email_cc: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    filename_tpl: Mapped[str] = mapped_column(String(200), default="report_{tgl}")
    queries: Mapped[List["JobQuery"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobQuery.position"
    )


class JobQuery(Base):
    __tablename__ = "job_queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(100), default="")
    sheet_name: Mapped[str] = mapped_column(String(31), default="Sheet1")
    connection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT"), nullable=True
    )
    sql: Mapped[str] = mapped_column(Text, default="")
    is_output: Mapped[bool] = mapped_column(Boolean, default=True)  # False = helper query, no sheet
    param_mode: Mapped[str] = mapped_column(String(10), default="none")  # none | date | list
    param_source_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    param_source_column: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="sql")  # sql | stage | merge | compare | pivot
    config: Mapped[str] = mapped_column(Text, default="")  # JSON pengaturan untuk stage / merge / compare / pivot
    job: Mapped[Job] = relationship(back_populates="queries")


class RunLog(Base):
    __tablename__ = "run_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual
    triggered_by: Mapped[str] = mapped_column(String(64), default="")
    send_email: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    date_from: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # rentang manual (YYYY-MM-DD)
    date_to: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | success | failed
    started_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    file_path: Mapped[str] = mapped_column(String(400), default="")
    file_name: Mapped[str] = mapped_column(String(300), default="")
    email_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # sent | skipped | failed
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")
    job: Mapped[Job] = relationship()


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
