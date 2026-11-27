"""Small, environment-independent stage-0 configuration boundary."""
import os
from dataclasses import dataclass
from pathlib import Path


def _cors_origins() -> tuple[str, ...]:
    configured = os.getenv("LAB_CORS_ORIGINS")
    if configured is not None:
        return tuple(origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip())
    return (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    )


@dataclass(frozen=True)
class Settings:
    app_name: str = "laboratory-instrument-selection"
    storage_dir: Path = Path("storage")
    database_path: Path = Path("data/app.sqlite3")
    cors_origins: tuple[str, ...] = _cors_origins()


settings = Settings()
