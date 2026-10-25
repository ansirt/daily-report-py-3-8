from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .database import SessionLocal
from .models import Job
from .runner import create_run, execute_run
from .utils import TZ_NAME, iso

scheduler = BackgroundScheduler(timezone=TZ_NAME)


def validate_cron(expr: str):
    CronTrigger.from_crontab(expr, timezone=TZ_NAME)  # raises ValueError


def _fire(job_id: int):
    try:
        execute_run(create_run(job_id, "schedule"))
    except Exception as e:  # job masih berjalan / dihapus
        print(f"[scheduler] job {job_id} dilewati: {e}")


def sync_jobs():
    for j in scheduler.get_jobs():
        scheduler.remove_job(j.id)
    with SessionLocal() as db:
        for job in db.scalars(select(Job).where(Job.enabled == True)):  # noqa: E712
            try:
                scheduler.add_job(_fire, CronTrigger.from_crontab(job.cron, timezone=TZ_NAME),
                                  id=f"job-{job.id}", args=[job.id], coalesce=True,
                                  max_instances=1, misfire_grace_time=3600)
            except ValueError as e:
                print(f"[scheduler] cron job '{job.name}' tidak valid: {e}")


def next_run(job_id: int):
    j = scheduler.get_job(f"job-{job_id}")
    return iso(j.next_run_time.replace(tzinfo=None)) if j and j.next_run_time else None
