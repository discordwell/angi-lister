from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.api import HealthResponse, ReadyResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
def healthz():
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=ReadyResponse)
def readyz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unavailable"

    # Worker heartbeat: check if any message was processed in last 5 minutes
    # or if there are no pending messages (worker has nothing to do = healthy)
    worker_status = "ok"  # TODO: check worker heartbeat table

    status = "ok" if db_status == "ok" and worker_status == "ok" else "degraded"
    return ReadyResponse(status=status, db=db_status, worker=worker_status)
