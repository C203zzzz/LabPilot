"""Health endpoint for the stage-0 runtime smoke check."""
from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "laboratory-instrument-selection"}
