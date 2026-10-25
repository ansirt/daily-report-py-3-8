from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .api import api, public
from .config import STATIC_DIR
from .database import SessionLocal, init_db
from .models import User
from .runner import recover_stale_runs
from .scheduler import scheduler, sync_jobs


def warn_if_no_admin():
    with SessionLocal() as db:
        if not db.scalar(select(func.count()).select_from(User)):
            print("=" * 60, "\n Belum ada user admin. Jalankan dulu:  py seed.py\n", "=" * 60, sep="")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    warn_if_no_admin()
    recover_stale_runs()
    scheduler.start()
    sync_jobs()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="DC Report Scheduler", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(public)
app.include_router(api)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
