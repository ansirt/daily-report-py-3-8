from __future__ import annotations

import os
import uvicorn

if __name__ == "__main__":
    # Jalankan SATU proses saja (scheduler berjalan di dalam proses ini) -> jangan pakai --workers > 1
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
