from fastapi.testclient import TestClient
from PIL import Image

from backend.domain.search import SearchResult
from backend.infrastructure.search_providers import (
    CCGPSearchProvider,
    DeepSeekGroundedSearchProvider,
    ExaMCPSearchProvider,
    PublicWebSearchProvider,
    SogouSearchProvider,
    TavilySearchProvider,
)
from backend.main import create_app
from backend.services.multimodal_ocr import MockMultimodalOCRAdapter
from backend.services.official_search import OfficialSearchService


def _task(client: TestClient) -> str:
    response = client.post("/api/v1/tasks", json={"scenario": "采购实验室仪器"})
    assert response.status_code == 201
    return response.json()["task"]["id"]


def test_browser_reader_flag_is_wired_into_shared_production_search_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_BROWSER_READING", "1")
    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")
    with TestClient(app):
        assert app.state.research_service.search.reader.browser is True


def test_upload_quote_category_is_returned_by_document_list(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            data={"category": "quote"},
            files={"file": ("quote.txt", "含税报价 12000 元", "text/plain")},
        )
        listed = client.get(f"/api/v1/tasks/{task_id}/documents")

    assert uploaded.status_code == 201
    assert uploaded.json()["source_name"] == "quote.txt"
    assert uploaded.json()["category"] == "quote"
    assert listed.status_code == 200
    assert listed.json()["items"][0]["source_name"] == "quote.txt"
    assert listed.json()["items"][0]["category"] == "quote"


def test_upload_rejects_unsupported_document_category(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        response = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            data={"category": "invoice"},
            files={"file": ("invoice.txt", "invoice", "text/plain")},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "invalid_document_category",
        "message": "资料类别不受支持",
        "field": "category",
    }


def test_ocr_rerun_preserves_document_category(tmp_path):
    image_path = tmp_path / "quote.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")
    app = create_app(
        database_path=tmp_path / "app.sqlite3",
        storage_dir=tmp_path / "storage",
        multimodal_adapter=MockMultimodalOCRAdapter(),
    )
    with TestClient(app) as client:
        task_id = _task(client)
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            data={"category": "quote"},
            files={"file": ("quote.png", image_path.read_bytes(), "image/png")},
        )
        response = client.post(
            f"/api/v1/tasks/{task_id}/documents/{uploaded.json()['id']}/ocr"
        )
        listed = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"]

    assert response.status_code == 200
    assert response.json()["category"] == "quote"
    assert listed[0]["category"] == "quote"


def test_cn_and_public_search_modes_activate_together_without_model_key(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        SogouSearchProvider,
        "search",
        lambda self, query: [SearchResult("https://example.cn/product", "中文产品页", provider=self.name)],
    )
    monkeypatch.setattr(
        PublicWebSearchProvider,
        "search",
        lambda self, query: [SearchResult("https://example.org/product", "Public product", provider=self.name)],
    )
    secret = "must-not-be-returned"
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post(
            "/api/v1/providers/search/health-check",
            json={"modes": ["cn", "public"], "api_key": secret},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "configured"
    assert response.json()["active_modes"] == ["cn", "public"]
    assert secret not in response.text

    with TestClient(create_app(database_path=tmp_path / "status.sqlite3", storage_dir=tmp_path / "status-storage")) as client:
        connected = client.post(
            "/api/v1/providers/search/health-check",
            json={"modes": ["cn", "public"]},
        )
        status = client.get("/api/v1/providers")

    assert connected.status_code == 200
    assert status.json()["status"] == "configured"
    assert status.json()["label"] == "部分已连接"
    search_item = next(item for item in status.json()["items"] if item["id"] == "search")
    assert search_item["status"] == "configured"
    assert search_item["modes"] == ["cn", "public"]


def test_failed_search_reconfiguration_clears_previous_runtime_modes(monkeypatch, tmp_path):
    rows = [SearchResult("https://example.org/product", "Public product", provider="public_web")]
    monkeypatch.setattr(PublicWebSearchProvider, "search", lambda self, query: list(rows))
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        first = client.post("/api/v1/providers/search/health-check", json={"modes": ["public"]})
        rows.clear()
        failed = client.post("/api/v1/providers/search/health-check", json={"modes": ["public"]})
        status = client.get("/api/v1/providers")

    assert first.json()["status"] == "configured"
    assert failed.json()["status"] == "unavailable"
    search_item = next(item for item in status.json()["items"] if item["id"] == "search")
    assert search_item["status"] == "ready_to_test"
    assert search_item["modes"] == []


def test_tavily_search_mode_does_not_require_model_key_or_echo_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        TavilySearchProvider,
        "search",
        lambda self, query: [SearchResult("https://example.org/tender", "Tender", provider=self.name)] if self.api_key else [],
    )
    secret = "tavily-runtime-secret"
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post(
            "/api/v1/providers/search/health-check",
            json={"modes": ["tavily"], "credentials": {"tavily": secret}},
        )

    assert response.status_code == 200
    assert response.json()["active_modes"] == ["tavily"]
    assert secret not in response.text


def test_search_health_rejects_unknown_mode_before_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post(
            "/api/v1/providers/search/health-check",
            json={"modes": ["browser"]},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_search_mode"


def test_default_workflow_search_reuses_runtime_provider_chain(monkeypatch, tmp_path):
    """The task-search route must not rebuild a chain that drops providers."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    class RuntimeProvider:
        name = "runtime_probe"
        stop_on_results = True

        def supports(self, query):
            return True

        def search(self, query):
            return [SearchResult("https://example.org/runtime", "Runtime result", provider=self.name)]

    monkeypatch.setattr(DeepSeekGroundedSearchProvider, "from_env", classmethod(lambda cls, **kwargs: RuntimeProvider()))
    monkeypatch.setattr(CCGPSearchProvider, "search", lambda self, query: [])
    monkeypatch.setattr(ExaMCPSearchProvider, "search", lambda self, query: [])
    monkeypatch.setattr(PublicWebSearchProvider, "search", lambda self, query: [])

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购实验室仪器"}).json()
        task_id = created["task"]["id"]
        requirement = {"scenario": "采购实验室仪器", "hard_constraints": [], "soft_constraints": [], "unknowns": []}
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        response = client.post(f"/api/v1/tasks/{task_id}/search", json={
            "query": "X100 official", "purpose": "official", "fetch_body": False,
        })

    assert response.status_code == 200
    source = response.json()["items"][0]["sources"][0]
    assert source["provider"] == "runtime_probe"


def test_search_health_reconfigures_shared_research_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        PublicWebSearchProvider,
        "search",
        lambda self, query: [SearchResult("https://example.org/product", "Public product", provider=self.name)],
    )
    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        response = client.post("/api/v1/providers/search/health-check", json={"modes": ["public"]})
        assert response.status_code == 200
        assert [provider.name for provider in app.state.research_service.search.providers] == ["public_web"]


def test_search_health_does_not_mutate_injected_search_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        PublicWebSearchProvider,
        "search",
        lambda self, query: [SearchResult("https://example.org/product", "Public product", provider=self.name)],
    )

    class InjectedProvider:
        name = "injected"

        def search(self, _query):
            return []

    injected = OfficialSearchService(providers=[InjectedProvider()])
    app = create_app(
        database_path=tmp_path / "app.sqlite3",
        storage_dir=tmp_path / "storage",
        search_service=injected,
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/providers/search/health-check", json={"modes": ["public"]})
        assert response.status_code == 200
        assert app.state.research_service.search is injected
        assert [provider.name for provider in injected.providers] == ["injected"]


def test_evaluate_api_uses_immutable_requirement_boundary(monkeypatch, tmp_path):
    from backend.domain.requirements_improved import RequirementSpec as HardenedRequirement
    import backend.services.evaluation_service as evaluation_service

    calls = []
    original = HardenedRequirement.from_dict

    class ProbeRequirement:
        @classmethod
        def from_dict(cls, data):
            calls.append(data)
            return original(data)

    monkeypatch.setattr(evaluation_service, "ImmutableRequirementSpec", ProbeRequirement)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        response = client.post(
            f"/api/v1/tasks/{task_id}/evaluate",
            json={
                "requirement": {
                    "scenario": "采购培养设备",
                    "status": "confirmed",
                    "hard_constraints": [{"name": "capacity", "value": {"min": 10}}],
                },
                "candidates": [{"id": "candidate-1", "model": "X100"}],
                "facts": [],
            },
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "needs_review"
    assert len(calls) == 1


def test_requirement_confirm_api_does_not_fallback_after_hardened_validation(monkeypatch, tmp_path):
    """Malformed requirements must be rejected by the API boundary."""
    from backend.domain.requirements_improved import RequirementSpec as HardenedRequirement
    import backend.services.requirement_service as requirement_service

    calls = []
    original = HardenedRequirement.from_dict

    class ProbeRequirement:
        @classmethod
        def from_dict(cls, data):
            calls.append(data)
            return original(data)

    monkeypatch.setattr(requirement_service, "ImmutableRequirementSpec", ProbeRequirement)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        response = client.post(
            f"/api/v1/tasks/{task_id}/requirements/confirm",
            json={"requirement": {"scenario": "采购设备", "version": 0}},
        )

    assert response.status_code == 422
    assert calls


def test_candidate_creation_api_uses_hardened_candidate_merge(monkeypatch, tmp_path):
    import backend.services.candidate_service as candidate_service

    calls = []
    original = candidate_service.merge_immutable_candidates

    def probe(items):
        calls.append(tuple(items))
        return original(items)

    monkeypatch.setattr(candidate_service, "merge_immutable_candidates", probe)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        # The production candidate route consumes persisted document pages;
        # inject one through the same repository-backed HTTP contract.
        response = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            data={"category": "product"},
            files={"file": ("product.txt", b"Brand X, model X-100", "text/plain")},
        )
        assert response.status_code == 201
        created = client.post(f"/api/v1/tasks/{task_id}/candidates")

    assert created.status_code == 200
    assert calls and calls[0]


def test_fact_review_api_uses_hardened_state_machine(tmp_path):
    from datetime import datetime, timezone

    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        task_id = _task(client)
        candidate_id, fact_id = "candidate-1", "fact-1"
        app.state.repository.put("candidates", candidate_id, {
            "id": candidate_id, "brand": "Brand X", "series": None,
            "model": "X-100", "configuration": None, "sources": [],
            "status": "confirmed", "note": None,
        }, task_id=task_id)
        app.state.repository.put("facts", fact_id, {
            "fact_id": fact_id, "candidate_id": candidate_id, "task_id": task_id,
            "field_name": "capacity", "value": 10, "value_type": "number",
            "unit": "samples", "status": "pending_review",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "evidence": [{
                "evidence_id": "evidence-1", "candidate_id": candidate_id,
                "source_type": "user_document", "authority_score": 90,
                "evidence_level": "user_provided",
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "document_id": "document-1", "location": "page:1",
                "quote": "X-100 capacity 10 samples",
            }],
        }, task_id=task_id)
        confirmed = client.post(
            f"/api/v1/tasks/{task_id}/facts/{fact_id}/review",
            json={"action": "confirm", "reviewer_id": "reviewer-1"},
        )
        repeated = client.post(
            f"/api/v1/tasks/{task_id}/facts/{fact_id}/review",
            json={"action": "confirm", "reviewer_id": "reviewer-1"},
        )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert repeated.status_code == 422


def test_search_api_uses_hardened_query_boundary(monkeypatch, tmp_path):
    import backend.domain.search_improved as search_improved

    calls = []
    original = search_improved.SearchQuery

    def probe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    class Provider:
        name = "probe"

        def search(self, _query):
            return []

    monkeypatch.setattr(search_improved, "SearchQuery", probe)
    app = create_app(
        database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage",
        search_service=OfficialSearchService(providers=[Provider()]),
    )
    with TestClient(app) as client:
        task_id = _task(client)
        requirement = {"scenario": "采购实验室仪器", "hard_constraints": [], "soft_constraints": [], "unknowns": []}
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        response = client.post(
            f"/api/v1/tasks/{task_id}/search",
            json={"query": "X-100 official", "purpose": "official", "fetch_body": False},
        )

    assert response.status_code == 200
    assert calls and calls[0]["text"] == "X-100 official"


def test_search_api_exposes_hardened_query_limit_as_validation_error(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = _task(client)
        requirement = {"scenario": "采购实验室仪器", "hard_constraints": [], "soft_constraints": [], "unknowns": []}
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        response = client.post(
            f"/api/v1/tasks/{task_id}/search",
            json={"query": "x" * 501, "purpose": "official", "fetch_body": False},
        )

    assert response.status_code == 422


def test_search_api_reuses_only_complete_body_cache(tmp_path):
    from backend.domain.search import SearchResult

    class Provider:
        name = "cache_probe"

        def __init__(self):
            self.searches = 0
            self.fetches = 0

        def supports(self, _query):
            return True

        def search(self, _query):
            self.searches += 1
            return [SearchResult("https://www.eppendorf.com/x100", "X100 official", provider=self.name)]

        def fetch(self, _url, _timeout):
            self.fetches += 1
            return "X100 official specifications"

    provider = Provider()
    search = OfficialSearchService(providers=[provider], fetch_body=True)
    with TestClient(create_app(
        database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage",
        search_service=search,
    )) as client:
        task_id = _task(client)
        requirement = {"scenario": "采购设备", "hard_constraints": [], "soft_constraints": []}
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        first = client.post(f"/api/v1/tasks/{task_id}/search", json={
            "query": "X100 official", "fetch_body": True,
        })
        second = client.post(f"/api/v1/tasks/{task_id}/search", json={
            "query": "X100 official", "fetch_body": True,
        })

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["id"] == second.json()["items"][0]["id"]
    assert provider.searches == provider.fetches == 1


def test_search_api_retries_transient_failed_body_instead_of_caching_it(tmp_path):
    from backend.domain.search import SearchResult

    class Provider:
        name = "retry_probe"

        def __init__(self):
            self.searches = 0
            self.fetches = 0

        def supports(self, _query):
            return True

        def search(self, _query):
            self.searches += 1
            return [SearchResult("https://www.eppendorf.com/x200", "X200 official", provider=self.name)]

        def fetch(self, _url, _timeout):
            self.fetches += 1
            if self.fetches == 1:
                raise TimeoutError("temporary")
            return "X200 official specifications"

    provider = Provider()
    search = OfficialSearchService(providers=[provider], fetch_body=True)
    with TestClient(create_app(
        database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage",
        search_service=search,
    )) as client:
        task_id = _task(client)
        requirement = {"scenario": "采购设备", "hard_constraints": [], "soft_constraints": []}
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        first = client.post(f"/api/v1/tasks/{task_id}/search", json={
            "query": "X200 official", "fetch_body": True,
        })
        second = client.post(f"/api/v1/tasks/{task_id}/search", json={
            "query": "X200 official", "fetch_body": True,
        })

    first_source = first.json()["items"][0]["sources"][0]
    second_source = second.json()["items"][0]["sources"][0]
    assert first_source["body_status"] == "timeout"
    assert second_source["body_status"] == "fetched"
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    assert provider.searches == provider.fetches == 2


def test_workflow_results_persist_across_app_reopen(tmp_path):
    database_path = tmp_path / "workflow.sqlite3"
    storage_dir = tmp_path / "storage"
    with TestClient(create_app(database_path=database_path, storage_dir=storage_dir)) as client:
        task_id = _task(client)
        requirement = {"scenario": "采购实验室仪器", "hard_constraints": [], "soft_constraints": []}
        candidates = [{"id": "candidate-1", "model": "M-1"}]
        evaluated = client.post(
            f"/api/v1/tasks/{task_id}/evaluate",
            json={"requirement": requirement, "candidates": candidates, "facts": []},
        )
        assert evaluated.status_code == 200
        assert evaluated.json()["items"][0]["status"] == "needs_review"
        assert evaluated.json()["items"][0]["hard_constraints"][0]["missing_reason"] == "no_requirement_constraints"
        ranking = client.post(
            f"/api/v1/tasks/{task_id}/recommend",
            json={"evaluations": evaluated.json()["items"], "facts": []},
        )
        assert ranking.status_code == 200
        assert ranking.json()["status"] == "no_recommendation"
        assert ranking.json()["preferred"] is None
        quote = {
            "quote_id": "quote-1", "candidate_id": "candidate-1", "supplier": "供应商甲",
            "model": "M-1", "configuration": "标准配置", "amount": 12000,
            "currency": "CNY", "source_type": "current_supplier_quote",
            "source": "报价单", "source_location": "报价单:p1",
        }
        comparison = client.post(
            f"/api/v1/tasks/{task_id}/compare",
            json={"candidate_id": "candidate-1", "quotes": [quote]},
        )
        assert comparison.status_code == 200
        questions = client.post(
            f"/api/v1/tasks/{task_id}/questions",
            json={
                "evaluations": evaluated.json()["items"], "facts": [], "quotes": [quote],
                "comparisons": [comparison.json()], "recommendations": ranking.json(),
            },
        )
        assert questions.status_code == 200
        state = client.get(f"/api/v1/tasks/{task_id}").json()["workflow_state"]
        assert state["evaluations"] == evaluated.json()["items"]
        assert state["ranking"] == ranking.json()
        assert state["quotes"] == [quote]
        assert state["comparison"] == comparison.json()
        assert state["generated_questions"] == questions.json()

    with TestClient(create_app(database_path=database_path, storage_dir=storage_dir)) as reopened:
        restored = reopened.get(f"/api/v1/tasks/{task_id}")
        assert restored.status_code == 200
        assert restored.json()["workflow_state"]["ranking"] == ranking.json()
        assert restored.json()["workflow_state"]["comparison"] == comparison.json()
