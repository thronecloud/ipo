"""WisdomInvest API — FastAPI read-layer over the living engine's Postgres store.

Run: uvicorn api.main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.deps import get_db  # noqa: F401  (re-exported for convenience/tests)
from api.routers import admin, consumer
from db.base import engine

app = FastAPI(
    title="WisdomInvest API",
    description="Read-layer for the WisdomInvest dashboards (Consumer Research Desk + Admin Engine Room).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(consumer.router)
app.include_router(admin.router)


@app.get("/health", tags=["meta"])
def health():
    """Liveness + DB connectivity check."""
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
