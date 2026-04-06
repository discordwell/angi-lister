import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.db.session import get_bypass_db
from app.models import OutboundMessage
from app.schemas.api import HealthResponse, ReadyResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
def healthz():
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=ReadyResponse)
def readyz(db: Session = Depends(get_bypass_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unavailable"

    # Worker heartbeat: check if there are stale pending messages
    # (pending for over 5 minutes means the worker is likely down)
    five_min_ago = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    stale_pending = (
        db.query(func.count(OutboundMessage.id))
        .filter(
            OutboundMessage.status == "pending",
            OutboundMessage.queued_at < five_min_ago,
        )
        .scalar()
        or 0
    )
    worker_status = "stale" if stale_pending > 0 else "ok"

    status = "ok" if db_status == "ok" and worker_status == "ok" else "degraded"
    return ReadyResponse(status=status, db=db_status, worker=worker_status)
