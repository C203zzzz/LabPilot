from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.services.candidate_service import extract_candidates
from backend.services.document_ingestion import DocumentIngestionService
from backend.services.requirement_service import RequirementService


ROOT = Path(__file__).parents[1]


def test_health_endpoint_is_available():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stage0_boundaries_and_existing_services_are_importable():
    assert (ROOT / "backend" / "api").is_dir()
    assert (ROOT / "backend" / "infrastructure").is_dir()
    assert (ROOT / "backend" / "repositories").is_dir()
    assert (ROOT / "frontend" / "src").is_dir()
    assert RequirementService and DocumentIngestionService and extract_candidates


def test_frontend_has_declared_directory_responsibilities():
    expected = {"app", "pages", "features", "components", "services", "styles"}
    assert expected <= {p.name for p in (ROOT / "frontend" / "src").iterdir()}


def test_no_marketlens_runtime_dependency_in_stage0_entrypoints():
    for relative in ["backend/main.py", "backend/api/health.py", "backend/infrastructure/config.py"]:
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "marketlens" not in text
