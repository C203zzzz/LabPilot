from fastapi.testclient import TestClient
from PIL import Image

from backend.main import create_app
from backend.services.multimodal_ocr import MockMultimodalOCRAdapter


def test_m02_ocr_endpoint_persists_pending_review_draft(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage", multimodal_adapter=MockMultimodalOCRAdapter())) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购灭菌器"}).json()["task"]["id"]
        uploaded = client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "not visual", "text/plain")})
        document_id = uploaded.json()["id"]
        result = client.post(f"/api/v1/tasks/{task_id}/documents/{document_id}/ocr")
        assert result.status_code == 200
        body = result.json()
        assert body["model_extraction"]["review_status"] == "pending_review"
        assert body["model_extraction"]["status"] == "failed"
        assert body["warnings"][-1]["code"] == "no_visual_input"


def test_m02_ocr_endpoint_requires_adapter(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购灭菌器"}).json()["task"]["id"]
        uploaded = client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.jpg", b"not-jpeg", "image/jpeg")})
        result = client.post(f"/api/v1/tasks/{task_id}/documents/{uploaded.json()['id']}/ocr")
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "ocr_provider_not_configured"


def test_m02_ocr_endpoint_persists_image_text_fields_and_locations(tmp_path):
    image_path = tmp_path / "ls-50hd.png"
    Image.new("RGB", (80, 60), "white").save(image_path, format="PNG")
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage", multimodal_adapter=MockMultimodalOCRAdapter())) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购灭菌器"}).json()["task"]["id"]
        uploaded = client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("ls-50hd.png", image_path.read_bytes(), "image/png")})
        document_id = uploaded.json()["id"]
        result = client.post(f"/api/v1/tasks/{task_id}/documents/{document_id}/ocr")
        assert result.status_code == 200
        body = result.json()
        assert body["images"][0]["ocr_status"] == "completed"
        assert body["images"][0]["ocr_text"]
        assert body["images"][0]["structured_fields"]["model"]["status"] == "pending_review"
        assert body["model_extraction"]["review_status"] == "pending_review"
        assert body["model_extraction"]["source_refs"][0]["location"].startswith("image:1")
